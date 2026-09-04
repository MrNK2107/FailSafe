"""
Integration tests proving diagnosis in the checkout-abandonment pipeline
is actually WIRED IN, not a standalone function that gets called and
ignored - mirrors tests/test_diagnosis_pipeline.py's own load-bearing
test exactly, applied to this domain. Uses the real in-process MCP
server, forced into SIMULATE mode regardless of local `.env` state (both
`mcp_server.SIMULATE` and `mcp_server._rp.simulate` - see
tests/test_diagnosis_pipeline.py's own docstring for why patching only
the first is not sufficient), so no live Razorpay call is ever a side
effect of running this suite.
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
from abandonment_gate import AbandonmentGate, MIN_CART_VALUE_FOR_ACTION_PAISE, STALE_ABANDONMENT_MINUTES_THRESHOLD
from audit_log import AuditLogger
from checkout_abandonment_agent import process_one


def _run(cart: dict, diagnosis_return=None, inject_failure=None):
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        audit_path = Path(f.name)
    audit = AuditLogger(audit_path)
    gate = AbandonmentGate()

    async def _go():
        async with Client(mcp_server.server) as client:
            return await process_one(client, gate, audit, cart, inject_failure=inject_failure)

    try:
        with patch.object(mcp_server, "SIMULATE", True), \
             patch.object(mcp_server._rp, "simulate", True):
            if diagnosis_return is not None:
                with patch("checkout_abandonment_agent.diagnose_abandonment_reason", return_value=diagnosis_return):
                    result = asyncio.run(_go())
            else:
                result = asyncio.run(_go())
        events = audit.read_all()
        return result, events
    finally:
        audit_path.unlink()
        mcp_server._reset_tool_level_guard_for_tests()


def _base_cart(**overrides):
    cart = {
        "cart_id": "cart_pipeline_test",
        "amount_paise": 29900,
        "item": "Test Item",
        "checkout_stage": "otp_entry",
        "minutes_since_abandonment": 3,
        "device_type": "mobile_web",
        "is_returning_customer": True,
        "abandonment_reason": "otp_delay_or_failure",  # ground truth: policy -> immediate_payment_link_resend
        "simulated_customer_response": True,
    }
    cart.update(overrides)
    return cart


def test_correct_diagnosis_drives_the_gate_and_a_real_tool_call():
    cart = _base_cart()
    result, events = _run(
        cart, diagnosis_return={"reason": "otp_delay_or_failure", "reasoning": "matches"},
    )
    assert result["diagnosed_reason"] == "otp_delay_or_failure"
    assert result["diagnosis_matched_ground_truth"] is True
    assert result["final_action"] == "immediate_payment_link_resend"
    assert result["gate_executed"] is True

    tool_calls = [e for e in events if e["event_type"] == "mcp_tool_call"]
    assert tool_calls[0]["tool"] == "create_payment_link"

    diag_events = [e for e in events if e["event_type"] == "abandonment_diagnosis"]
    assert len(diag_events) == 1
    assert diag_events[0]["diagnosed_reason"] == "otp_delay_or_failure"
    assert diag_events[0]["diagnosis_matched_ground_truth"] is True


def test_wrong_diagnosis_changes_the_final_action_real_downstream_consequences():
    # Ground truth is otp_delay_or_failure (policy: immediate_payment_link_resend),
    # but diagnosis is mocked to (wrongly) diagnose trust_or_security_concern
    # (policy: no_action_respect_hesitation) - a genuinely different action.
    # This proves the gate acts on the DIAGNOSED reason, not ground truth.
    cart = _base_cart()
    result, events = _run(
        cart, diagnosis_return={"reason": "trust_or_security_concern", "reasoning": "misdiagnosed"},
    )
    assert result["diagnosed_reason"] == "trust_or_security_concern"
    assert result["diagnosis_matched_ground_truth"] is False
    assert result["final_action"] == "no_action_respect_hesitation"

    tool_calls = [e for e in events if e["event_type"] == "mcp_tool_call"]
    assert tool_calls[0]["tool"] == "flag_for_manual_review"  # NOT create_payment_link


def test_diagnosis_failure_flags_for_manual_review_without_reaching_gate():
    cart = _base_cart()
    result, events = _run(cart, diagnosis_return={"reason": None, "reasoning": "Model returned no tool call."})

    assert result["final_action"] == "no_action_needs_human_review"
    assert result["gate_executed"] is True
    assert result["diagnosed_reason"] is None
    assert result["diagnosis_matched_ground_truth"] is False

    event_types = [e["event_type"] for e in events]
    assert "abandonment_diagnosis_failed" in event_types
    assert "abandonment_gate_decision" not in event_types  # never reached the gate

    review_call = next(e for e in events if e["event_type"] == "mcp_tool_call")
    assert review_call["tool"] == "flag_for_manual_review"


def test_diagnosis_returning_unrecognized_reason_is_treated_as_failure():
    cart = _base_cart()
    result, events = _run(cart, diagnosis_return={"reason": "aliens_intervened", "reasoning": "hallucinated"})

    assert result["final_action"] == "no_action_needs_human_review"
    assert result["diagnosed_reason"] == "aliens_intervened"
    assert result["diagnosis_matched_ground_truth"] is False
    event_types = [e["event_type"] for e in events]
    assert "abandonment_diagnosis_failed" in event_types
    assert "abandonment_gate_decision" not in event_types


def test_injected_diagnosis_parse_failure_exercises_the_real_failure_path():
    cart = _base_cart()
    result, events = _run(cart, inject_failure="diagnosis_parse_failure")
    assert result["diagnosed_reason"] is None
    assert result["diagnosis_matched_ground_truth"] is False
    event_types = [e["event_type"] for e in events]
    assert "abandonment_diagnosis_failed" in event_types
    assert "abandonment_gate_decision" not in event_types


def test_low_value_cart_never_gets_a_nudge_even_with_a_correctly_diagnosed_reason():
    cart = _base_cart(amount_paise=MIN_CART_VALUE_FOR_ACTION_PAISE - 1)
    result, events = _run(
        cart, diagnosis_return={"reason": "otp_delay_or_failure", "reasoning": "matches"},
    )
    assert result["diagnosis_matched_ground_truth"] is True  # diagnosis was RIGHT
    assert result["final_action"] == "no_action_low_value"  # but the gate still skips it

    tool_calls = [e for e in events if e["event_type"] == "mcp_tool_call"]
    assert tool_calls[0]["tool"] == "flag_for_manual_review"


def test_stale_abandonment_never_gets_a_nudge_even_with_a_correctly_diagnosed_reason():
    cart = _base_cart(minutes_since_abandonment=STALE_ABANDONMENT_MINUTES_THRESHOLD)
    result, events = _run(
        cart, diagnosis_return={"reason": "otp_delay_or_failure", "reasoning": "matches"},
    )
    assert result["diagnosis_matched_ground_truth"] is True
    assert result["final_action"] == "no_action_stale_abandonment"

    tool_calls = [e for e in events if e["event_type"] == "mcp_tool_call"]
    assert tool_calls[0]["tool"] == "flag_for_manual_review"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
