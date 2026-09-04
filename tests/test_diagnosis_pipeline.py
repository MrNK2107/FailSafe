"""
Integration tests proving diagnosis is actually WIRED into the pipeline,
not just a standalone function that gets called and ignored - the
distinction PS_REQUIREMENTS_DEBATE.md's task explicitly warns about
("a stage that 'diagnoses' but is then ignored by the rest of the
pipeline does not fix this gap"). These tests run process_record()
end-to-end with a mocked diagnose_decline_code() and check that:

  1. A CORRECT diagnosis feeds the diagnosed code into gate.evaluate()
     (proven by checking the final_action matches the diagnosed code's
     policy, and diagnosis_matched_ground_truth is True).
  2. A WRONG diagnosis (a different, valid decline_code than ground
     truth) changes what the gate does - it evaluates against the WRONG
     policy row, producing a materially different final_action than
     ground truth's own policy would give. This is the "real downstream
     consequences" requirement, tested directly rather than asserted.
  3. A diagnosis FAILURE (no tool call / unrecognized code) is handled
     gracefully - flagged for manual review, gate and action-proposal LLM
     never reached - mirroring test_agent_unknown_code.py's assertions.
  4. A record with no raw-signal field (or raw_signal_field not passed)
     falls through to the pre-diagnosis behavior unchanged - proves
     existing callers (agent_onetime.py, and every pre-existing test
     fixture) are unaffected by this feature.

Uses the real in-process MCP server, forced into SIMULATE mode regardless
of local `.env` state, so no live Razorpay call is a side effect of
running the suite - test_correct_diagnosis_drives_the_gate below reaches a
real money-moving tool (create_retry_order), not just flag_for_manual_review,
so this isn't optional here the way it might look in a test that only ever
hits the no-action path. Forces BOTH `mcp_server.SIMULATE` and
`mcp_server._rp.simulate` - see diagnosis_live_demo.py's docstring for why
patching only the first one is NOT sufficient when real `rzp_test_` keys
are present in `.env` (found the hard way while building this feature).
"""

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from mcp import Client

import mcp_server
from audit_log import AuditLogger
from decline_codes import RecoveryAction
from gate import Gate
from recovery_pipeline import process_record

ACTION_TO_TOOL = {
    RecoveryAction.IMMEDIATE_RETRY: "create_retry_order",
    RecoveryAction.DELAYED_RETRY: "create_retry_order",
    RecoveryAction.PAYMENT_LINK_NUDGE: "create_payment_link",
    RecoveryAction.NO_ACTION_FRAUD: "flag_for_manual_review",
    RecoveryAction.NO_ACTION_UNRECOVERABLE: "flag_for_manual_review",
}


def _run(record: dict, raw_signal_field: str | None, diagnosis_return=None, inject_failure=None):
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        audit_path = Path(f.name)
    audit = AuditLogger(audit_path)
    gate = Gate()

    async def _go():
        async with Client(mcp_server.server) as client:
            return await process_record(
                client, gate, audit, record,
                id_field="subscription_id",
                action_to_tool=ACTION_TO_TOOL,
                item_label_field="plan",
                situation="test situation",
                record_label="Subscription",
                raw_signal_field=raw_signal_field,
                inject_failure=inject_failure,
            )

    try:
        with patch.object(mcp_server, "SIMULATE", True), \
             patch.object(mcp_server._rp, "simulate", True):
            if diagnosis_return is not None:
                with patch("recovery_pipeline.diagnose_decline_code", return_value=diagnosis_return):
                    result = asyncio.run(_go())
            else:
                result = asyncio.run(_go())
        events = audit.read_all()
        return result, events
    finally:
        audit_path.unlink()
        mcp_server._reset_tool_level_guard_for_tests()


def _base_record(**overrides):
    record = {
        "subscription_id": "sub_diag_test",
        "amount_paise": 29900,
        "decline_code": "insufficient_funds",  # ground truth: policy -> delayed_retry
        "raw_decline_message": "Bank response: insufficient balance in account.",
        "plan": "Test Plan",
        "halted_days_ago": 1,
        "simulated_customer_response": False,
    }
    record.update(overrides)
    return record


def test_correct_diagnosis_drives_the_gate_and_is_logged_as_matched():
    record = _base_record()
    result, events = _run(
        record, raw_signal_field="raw_decline_message",
        diagnosis_return={"decline_code": "insufficient_funds", "reasoning": "matches insufficient funds"},
        inject_failure="llm_parse_failure",  # skip the real Ollama action-proposal call
    )

    assert result["diagnosed_decline_code"] == "insufficient_funds"
    assert result["diagnosis_matched_ground_truth"] is True
    # The gate always executes the POLICY action regardless of what the
    # (here injected-to-fail) action-proposal LLM said - insufficient_funds
    # -> delayed_retry - proving the gate looked up the diagnosed code's
    # policy row correctly. The wrong-diagnosis test below proves this
    # same lookup uses the DIAGNOSED code, not ground truth, when they
    # differ.
    assert result["final_action"] == "delayed_retry"
    diagnosis_events = [e for e in events if e["event_type"] == "diagnosis"]
    assert len(diagnosis_events) == 1
    assert diagnosis_events[0]["diagnosed_decline_code"] == "insufficient_funds"
    assert diagnosis_events[0]["true_decline_code"] == "insufficient_funds"
    assert diagnosis_events[0]["diagnosis_matched_ground_truth"] is True

    gate_events = [e for e in events if e["event_type"] == "gate_decision"]
    assert len(gate_events) == 1
    assert gate_events[0]["decline_code"] == "insufficient_funds"
    assert gate_events[0]["true_decline_code"] == "insufficient_funds"


def test_wrong_diagnosis_changes_the_final_action_real_downstream_consequences():
    # Ground truth is insufficient_funds (policy: delayed_retry), but the
    # diagnosis stage is mocked to (wrongly) diagnose debit_instrument_blocked
    # (policy: no_action_unrecoverable) - a genuinely different action.
    # This proves the gate acts on the DIAGNOSED code, not ground truth:
    # if it acted on ground truth, final_action would reflect
    # insufficient_funds's policy regardless of what diagnosis said.
    record = _base_record()
    result, events = _run(
        record, raw_signal_field="raw_decline_message",
        diagnosis_return={"decline_code": "debit_instrument_blocked", "reasoning": "misdiagnosed as blocked"},
        inject_failure="llm_parse_failure",
    )

    assert result["diagnosed_decline_code"] == "debit_instrument_blocked"
    assert result["diagnosis_matched_ground_truth"] is False
    # debit_instrument_blocked is a no-action policy - the gate must have
    # evaluated against THAT policy, not insufficient_funds's delayed_retry.
    assert result["final_action"] == "no_action_unrecoverable"

    gate_events = [e for e in events if e["event_type"] == "gate_decision"]
    assert gate_events[0]["decline_code"] == "debit_instrument_blocked"
    assert gate_events[0]["true_decline_code"] == "insufficient_funds"
    assert gate_events[0]["diagnosis_matched_ground_truth"] is False

    # No flag_for_manual_review reason should reference insufficient_funds's
    # policy path (delayed_retry/create_retry_order) - the executed tool
    # call must be the one driven by the diagnosed code's no-action policy.
    tool_calls = [e for e in events if e["event_type"] == "mcp_tool_call"]
    assert tool_calls[0]["tool"] == "flag_for_manual_review"


def test_diagnosis_failure_flags_for_manual_review_without_reaching_gate():
    record = _base_record()
    result, events = _run(
        record, raw_signal_field="raw_decline_message",
        diagnosis_return={"decline_code": None, "reasoning": "Model returned no tool call."},
    )

    assert result["final_action"] == "no_action_unrecoverable"
    assert result["gate_executed"] is True
    assert result["diagnosed_decline_code"] is None
    assert result["diagnosis_matched_ground_truth"] is False

    event_types = [e["event_type"] for e in events]
    assert "diagnosis_failed" in event_types
    assert "gate_decision" not in event_types  # never reached the gate

    review_call = next(e for e in events if e["event_type"] == "mcp_tool_call")
    assert review_call["tool"] == "flag_for_manual_review"


def test_diagnosis_returning_unrecognized_code_is_treated_as_failure():
    record = _base_record()
    result, events = _run(
        record, raw_signal_field="raw_decline_message",
        diagnosis_return={"decline_code": "not_a_real_code", "reasoning": "hallucinated"},
    )

    assert result["final_action"] == "no_action_unrecoverable"
    assert result["diagnosed_decline_code"] == "not_a_real_code"
    assert result["diagnosis_matched_ground_truth"] is False
    event_types = [e["event_type"] for e in events]
    assert "diagnosis_failed" in event_types
    assert "gate_decision" not in event_types


def test_injected_diagnosis_parse_failure_exercises_the_real_failure_path():
    record = _base_record()
    result, events = _run(
        record, raw_signal_field="raw_decline_message",
        inject_failure="diagnosis_parse_failure",
    )
    assert result["diagnosed_decline_code"] is None
    assert result["diagnosis_matched_ground_truth"] is False
    event_types = [e["event_type"] for e in events]
    assert "diagnosis_failed" in event_types
    assert "gate_decision" not in event_types


def test_no_raw_signal_field_falls_through_to_pre_diagnosis_behavior():
    # raw_signal_field not passed at all (None) - existing callers
    # (agent_onetime.py) and every pre-existing test fixture must behave
    # exactly as they did before this feature existed: ground-truth
    # decline_code used directly, no diagnosis event logged at all.
    record = _base_record()
    result, events = _run(record, raw_signal_field=None, inject_failure="llm_parse_failure")

    assert result["diagnosed_decline_code"] is None
    assert result["diagnosis_matched_ground_truth"] is None
    event_types = [e["event_type"] for e in events]
    assert "diagnosis" not in event_types
    assert "diagnosis_failed" not in event_types
    gate_events = [e for e in events if e["event_type"] == "gate_decision"]
    assert gate_events[0]["decline_code"] == "insufficient_funds"


def test_raw_signal_field_present_but_record_missing_the_field_falls_through():
    # raw_signal_field is passed, but this particular record has no such
    # key (e.g. a malformed record) - must not KeyError, must fall through
    # to ground truth exactly like raw_signal_field=None.
    record = _base_record()
    del record["raw_decline_message"]
    result, events = _run(record, raw_signal_field="raw_decline_message", inject_failure="llm_parse_failure")

    assert result["diagnosed_decline_code"] is None
    event_types = [e["event_type"] for e in events]
    assert "diagnosis" not in event_types
    assert "diagnosis_failed" not in event_types


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
