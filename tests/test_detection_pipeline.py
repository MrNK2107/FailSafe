"""
Integration tests proving revenue-at-risk DETECTION is actually WIRED into
the pipeline, not just a standalone function that gets called and ignored
- the same "theater" warning PS_REQUIREMENTS_DEBATE.md's task raised for
diagnosis (Round 2), now applied to detection (finding 4). These tests run
process_record() end-to-end with a mocked detect_at_risk() and check that:

  1. A CORRECT "leave_alone" call on a genuinely healthy record stops the
     record immediately - no diagnosis, no gate, no MCP tool call at all.
  2. A CORRECT "needs_recovery_attention" call on a genuinely at-risk
     record lets it proceed through diagnosis/the gate/execution exactly
     as it would with detection turned off.
  3. A WRONG "leave_alone" call on a genuinely AT-RISK record has a real
     consequence: that record is never diagnosed, never gated, and never
     recovered - the exact "at-risk subscription wrongly cleared should
     NOT get recovered" requirement, proven directly rather than asserted.
  4. A WRONG "needs_recovery_attention" call on a genuinely HEALTHY record
     also has a real consequence: it genuinely proceeds into the rest of
     the pipeline (a healthy record has no decline_code, so it lands on
     the existing unknown_decline_code path and a REAL
     flag_for_manual_review tool call fires for a customer who needed no
     such thing) - wasted pipeline work on a fine customer, not a
     cosmetic mislabel.
  5. A detection FAILURE (no tool call) fails safe by proceeding into the
     rest of the pipeline rather than silently dropping the record.
  6. run_detection=False (the default) leaves every existing caller
     completely unaffected - no detection event is ever logged.

Uses the real in-process MCP server, forced into SIMULATE mode regardless
of local `.env` state, matching test_diagnosis_pipeline.py's exact
precaution (mcp_server.SIMULATE AND mcp_server._rp.simulate - patching
only the first is insufficient, see diagnosis_live_demo.py's docstring).
"""

import asyncio
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from mcp import Client

import mcp_server
from audit_log import AuditLogger
from decline_codes import RecoveryAction
from gate import Gate
from recovery_pipeline import LEFT_ALONE_BY_DETECTION, process_record

ACTION_TO_TOOL = {
    RecoveryAction.IMMEDIATE_RETRY: "create_retry_order",
    RecoveryAction.DELAYED_RETRY: "create_retry_order",
    RecoveryAction.PAYMENT_LINK_NUDGE: "create_payment_link",
    RecoveryAction.NO_ACTION_FRAUD: "flag_for_manual_review",
    RecoveryAction.NO_ACTION_UNRECOVERABLE: "flag_for_manual_review",
}


def _run(record: dict, detection_return=None, diagnosis_return=None, run_detection=True, inject_failure=None):
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
                raw_signal_field="raw_decline_message",
                run_detection=run_detection,
                inject_failure=inject_failure,
            )

    detect_patch = (
        patch("recovery_pipeline.detect_at_risk", return_value=detection_return)
        if detection_return is not None else nullcontext()
    )
    diagnose_patch = (
        patch("recovery_pipeline.diagnose_decline_code", return_value=diagnosis_return)
        if diagnosis_return is not None else nullcontext()
    )
    try:
        with patch.object(mcp_server, "SIMULATE", True), \
             patch.object(mcp_server._rp, "simulate", True), \
             detect_patch, diagnose_patch:
            result = asyncio.run(_go())
        events = audit.read_all()
        return result, events
    finally:
        audit_path.unlink()
        mcp_server._reset_tool_level_guard_for_tests()


def _healthy_record(**overrides):
    record = {
        "subscription_id": "sub_healthy_test",
        "amount_paise": 29900,
        "decline_code": None,
        "raw_decline_message": None,
        "plan": "Test Plan",
        "halted_days_ago": None,
        "simulated_customer_response": False,
        "previous_retry_count": 0,
        "days_since_last_successful_charge": 1,
        "most_recent_gateway_response": None,
        "subscription_status": "active",
        "ground_truth_needs_attention": False,
    }
    record.update(overrides)
    return record


def _at_risk_record(**overrides):
    record = {
        "subscription_id": "sub_at_risk_test",
        "amount_paise": 149900,
        "decline_code": "insufficient_funds",  # policy -> delayed_retry
        "raw_decline_message": "Bank response: insufficient balance in account.",
        "plan": "Test Plan",
        "halted_days_ago": None,
        "simulated_customer_response": False,
        "previous_retry_count": 2,
        "days_since_last_successful_charge": 12,
        "most_recent_gateway_response": "Bank response: insufficient balance in account.",
        "subscription_status": "pending",
        "ground_truth_needs_attention": True,
    }
    record.update(overrides)
    return record


def test_correct_leave_alone_stops_a_healthy_record_before_diagnosis_or_gate():
    record = _healthy_record()
    result, events = _run(
        record, detection_return={"classification": "leave_alone", "reasoning": "clean signals"},
    )

    assert result["final_action"] == LEFT_ALONE_BY_DETECTION
    assert result["gate_executed"] is False
    assert result["detection_classification"] == "leave_alone"
    assert result["detection_matched_ground_truth"] is True

    event_types = [e["event_type"] for e in events]
    assert "detection_decision" in event_types
    assert "diagnosis" not in event_types
    assert "gate_decision" not in event_types
    assert "mcp_tool_call" not in event_types
    assert "unknown_decline_code" not in event_types


def test_correct_needs_attention_lets_an_at_risk_record_proceed_normally():
    record = _at_risk_record()
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
                raw_signal_field="raw_decline_message",
                run_detection=True,
                inject_failure="llm_parse_failure",  # skip the real action-proposal Ollama call
            )

    try:
        with patch.object(mcp_server, "SIMULATE", True), \
             patch.object(mcp_server._rp, "simulate", True), \
             patch("recovery_pipeline.detect_at_risk",
                   return_value={"classification": "needs_recovery_attention", "reasoning": "real trouble"}), \
             patch("recovery_pipeline.diagnose_decline_code",
                   return_value={"decline_code": "insufficient_funds", "reasoning": "matches insufficient funds"}):
            result = asyncio.run(_go())
        events = audit.read_all()
    finally:
        audit_path.unlink()
        mcp_server._reset_tool_level_guard_for_tests()

    assert result["detection_classification"] == "needs_recovery_attention"
    assert result["detection_matched_ground_truth"] is True
    # Diagnosis, the gate, and execution all ran normally - exactly the
    # same downstream behavior as with detection turned off entirely.
    assert result["diagnosed_decline_code"] == "insufficient_funds"
    assert result["diagnosis_matched_ground_truth"] is True
    assert result["final_action"] == "delayed_retry"
    event_types = [e["event_type"] for e in events]
    assert "detection_decision" in event_types
    assert "diagnosis" in event_types
    assert "gate_decision" in event_types
    assert "mcp_tool_call" in event_types


def test_wrong_leave_alone_on_at_risk_record_means_it_is_never_recovered():
    # The core "real consequence" proof for a false NEGATIVE: an actually
    # at-risk record gets wrongly cleared by detection. It must never reach
    # diagnosis, the gate, or any MCP tool call - the revenue is genuinely
    # never attempted, not just mislabeled.
    record = _at_risk_record()
    result, events = _run(
        record,
        detection_return={"classification": "leave_alone", "reasoning": "wrongly judged healthy"},
    )

    assert result["final_action"] == LEFT_ALONE_BY_DETECTION
    assert result["gate_executed"] is False
    assert result["detection_classification"] == "leave_alone"
    # Ground truth says this WAS at-risk - the detection call was wrong.
    assert result["detection_matched_ground_truth"] is False

    event_types = [e["event_type"] for e in events]
    assert "diagnosis" not in event_types
    assert "gate_decision" not in event_types
    assert "mcp_tool_call" not in event_types
    # No retry/nudge was ever created for this genuinely at-risk subscription.


def test_wrong_needs_attention_on_healthy_record_wastes_a_real_manual_review_call():
    # The core "real consequence" proof for a false POSITIVE: a genuinely
    # healthy record gets wrongly flagged. It must genuinely proceed into
    # the rest of the pipeline - since it has no decline_code, it lands on
    # the pre-existing unknown_decline_code path and a REAL
    # flag_for_manual_review MCP tool call fires, for a customer who never
    # needed one. This is a real, wasted, executed tool call - not a
    # cosmetic label that gets logged and ignored.
    record = _healthy_record()
    result, events = _run(
        record,
        detection_return={"classification": "needs_recovery_attention", "reasoning": "wrongly judged at-risk"},
    )

    assert result["final_action"] != LEFT_ALONE_BY_DETECTION
    assert result["detection_classification"] == "needs_recovery_attention"
    # Ground truth says this was healthy - the detection call was wrong.
    assert result["detection_matched_ground_truth"] is False

    event_types = [e["event_type"] for e in events]
    assert "unknown_decline_code" in event_types
    tool_calls = [e for e in events if e["event_type"] == "mcp_tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool"] == "flag_for_manual_review"


def test_non_enum_classification_scores_consistently_with_what_the_pipeline_actually_does():
    # Regression guard: detect_at_risk() deliberately doesn't validate its
    # own output against the two known classification strings (mirrors
    # diagnose_decline_code()'s same choice) - a hallucinated/garbage value
    # falls through the `== LEAVE_ALONE` check and proceeds into the
    # pipeline exactly like "needs_recovery_attention" would. The scoring
    # of detection_matched_ground_truth must agree with that real behavior
    # (record_proceeds = classification != LEAVE_ALONE), not re-check the
    # literal string "needs_recovery_attention" - otherwise a healthy
    # record that proceeds anyway (wasting a real manual-review call) could
    # silently log a false "matched" instead of the miss it actually is.
    record = _healthy_record()
    result, events = _run(
        record,
        detection_return={"classification": "not_a_real_classification", "reasoning": "garbage model output"},
    )

    # Proceeds exactly like needs_recovery_attention would - the real,
    # observable pipeline consequence.
    assert result["final_action"] != LEFT_ALONE_BY_DETECTION
    tool_calls = [e for e in events if e["event_type"] == "mcp_tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool"] == "flag_for_manual_review"

    # Ground truth says this was healthy, and it wasted a real manual-review
    # call anyway - that's a miss, and the audit log must say so, not "matched".
    assert result["detection_matched_ground_truth"] is False


def test_detection_failure_fails_safe_by_proceeding_not_by_silently_dropping():
    record = _at_risk_record()
    # inject_failure="diagnosis_parse_failure" keeps the downstream
    # diagnosis stage from making a real Ollama call in this test - it
    # only tests that DETECTION's own failure doesn't drop the record, not
    # what diagnosis itself does with it.
    result, events = _run(
        record,
        detection_return={"classification": None, "reasoning": "Model returned no tool call."},
        inject_failure="diagnosis_parse_failure",
    )

    # Detection failed, but the record was NOT silently dropped - it fell
    # through into the rest of the pipeline (diagnosis/gate), same as if
    # detection had said "needs_recovery_attention".
    assert result["final_action"] != LEFT_ALONE_BY_DETECTION
    event_types = [e["event_type"] for e in events]
    assert "detection_failed" in event_types
    assert "detection_decision" not in event_types
    # Proceeded far enough to reach (and fail) the diagnosis stage - proof
    # the record kept moving through the pipeline rather than vanishing.
    assert "diagnosis_failed" in event_types


def test_injected_detection_parse_failure_exercises_the_real_failure_path():
    record = _at_risk_record()
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
                raw_signal_field="raw_decline_message",
                run_detection=True,
                inject_failure="detection_parse_failure",
            )

    try:
        with patch.object(mcp_server, "SIMULATE", True), \
             patch.object(mcp_server._rp, "simulate", True), \
             patch("recovery_pipeline.diagnose_decline_code",
                   return_value={"decline_code": "insufficient_funds", "reasoning": "matches"}), \
             patch("recovery_pipeline.propose_action",
                   return_value={"action": "delayed_retry", "reasoning": "matches policy"}):
            result = asyncio.run(_go())
        events = audit.read_all()
    finally:
        audit_path.unlink()
        mcp_server._reset_tool_level_guard_for_tests()

    assert result["final_action"] != LEFT_ALONE_BY_DETECTION
    event_types = [e["event_type"] for e in events]
    assert "detection_failed" in event_types


def test_run_detection_false_leaves_existing_behavior_completely_unaffected():
    # The backward-compatibility guarantee: every pre-existing caller
    # (agent.py without opting in, agent_onetime.py, every pre-existing
    # test fixture) must behave exactly as before this feature existed.
    record = _at_risk_record()
    result, events = _run(
        record, run_detection=False, inject_failure="llm_parse_failure",
        diagnosis_return={"decline_code": "insufficient_funds", "reasoning": "matches"},
    )

    assert result["detection_classification"] is None
    assert result["detection_matched_ground_truth"] is None
    event_types = [e["event_type"] for e in events]
    assert "detection_decision" not in event_types
    assert "detection_failed" not in event_types


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
