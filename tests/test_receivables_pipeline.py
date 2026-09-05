"""
Integration tests proving diagnosis in the overdue-receivables pipeline is
actually WIRED IN, not a standalone function that gets called and ignored
- mirrors tests/test_diagnosis_pipeline.py's and
tests/test_checkout_abandonment_pipeline.py's own load-bearing test
exactly, applied to this domain. Uses the real in-process MCP server,
forced into SIMULATE mode regardless of local `.env` state (both
`mcp_server.SIMULATE` and `mcp_server._rp.simulate`), so no live Razorpay
call is ever a side effect of running this suite.
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
from receivables_agent import process_one
from receivables_gate import DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD, MAX_REMINDERS_BEFORE_ESCALATION, ReceivableGate


def _run(invoice: dict, diagnosis_return=None, inject_failure=None):
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        audit_path = Path(f.name)
    audit = AuditLogger(audit_path)
    gate = ReceivableGate()

    async def _go():
        async with Client(mcp_server.server) as client:
            return await process_one(client, gate, audit, invoice, inject_failure=inject_failure)

    try:
        with patch.object(mcp_server, "SIMULATE", True), \
             patch.object(mcp_server._rp, "simulate", True):
            if diagnosis_return is not None:
                with patch("receivables_agent.diagnose_receivable", return_value=diagnosis_return):
                    result = asyncio.run(_go())
            else:
                result = asyncio.run(_go())
        events = audit.read_all()
        return result, events
    finally:
        audit_path.unlink()
        mcp_server._reset_tool_level_guard_for_tests()


def _base_invoice(**overrides):
    invoice = {
        "invoice_id": "inv_pipeline_test",
        "amount_paise": 45000,
        "business_name": "Test Business Pvt Ltd",
        "days_overdue": 5,
        "payment_terms": "net_30",
        "customer_payment_history_signal": "first_time_overdue",
        "reminders_sent_count": 0,
        "last_reminder_response": None,
        "typical_order_amount_paise": 45000,
        "amount_vs_typical_ratio": 1.0,
        "case_reason": "payment_process_friction",  # ground truth: policy -> friendly_reminder
        "simulated_customer_response": True,
    }
    invoice.update(overrides)
    return invoice


def test_correct_diagnosis_drives_the_gate_and_a_real_tool_call():
    invoice = _base_invoice()
    result, events = _run(
        invoice, diagnosis_return={"case_reason": "payment_process_friction", "reasoning": "matches"},
    )
    assert result["diagnosed_case_reason"] == "payment_process_friction"
    assert result["diagnosis_matched_ground_truth"] is True
    assert result["final_action"] == "friendly_reminder"
    assert result["gate_executed"] is True

    tool_calls = [e for e in events if e["event_type"] == "mcp_tool_call"]
    assert tool_calls[0]["tool"] == "create_payment_link"

    diag_events = [e for e in events if e["event_type"] == "receivable_diagnosis"]
    assert len(diag_events) == 1
    assert diag_events[0]["diagnosed_case_reason"] == "payment_process_friction"
    assert diag_events[0]["diagnosis_matched_ground_truth"] is True


def test_wrong_diagnosis_changes_the_final_action_real_downstream_consequences():
    # Ground truth is payment_process_friction (policy: friendly_reminder),
    # but diagnosis is mocked to (wrongly) diagnose invoice_dispute_likely
    # (policy: no_action_needs_dispute_review) - a genuinely different
    # action. This proves the gate acts on the DIAGNOSED reason, not
    # ground truth.
    invoice = _base_invoice()
    result, events = _run(
        invoice, diagnosis_return={"case_reason": "invoice_dispute_likely", "reasoning": "misdiagnosed"},
    )
    assert result["diagnosed_case_reason"] == "invoice_dispute_likely"
    assert result["diagnosis_matched_ground_truth"] is False
    assert result["final_action"] == "no_action_needs_dispute_review"

    tool_calls = [e for e in events if e["event_type"] == "mcp_tool_call"]
    assert tool_calls[0]["tool"] == "flag_for_manual_review"  # NOT create_payment_link


def test_diagnosis_failure_flags_for_manual_review_without_reaching_gate():
    invoice = _base_invoice()
    result, events = _run(invoice, diagnosis_return={"case_reason": None, "reasoning": "Model returned no tool call."})

    assert result["final_action"] == "no_action_needs_human_review"
    assert result["gate_executed"] is True
    assert result["diagnosed_case_reason"] is None
    assert result["diagnosis_matched_ground_truth"] is False

    event_types = [e["event_type"] for e in events]
    assert "receivable_diagnosis_failed" in event_types
    assert "receivable_gate_decision" not in event_types  # never reached the gate

    review_call = next(e for e in events if e["event_type"] == "mcp_tool_call")
    assert review_call["tool"] == "flag_for_manual_review"


def test_diagnosis_returning_unrecognized_case_reason_is_treated_as_failure():
    invoice = _base_invoice()
    result, events = _run(invoice, diagnosis_return={"case_reason": "aliens_intervened", "reasoning": "hallucinated"})

    assert result["final_action"] == "no_action_needs_human_review"
    assert result["diagnosed_case_reason"] == "aliens_intervened"
    assert result["diagnosis_matched_ground_truth"] is False
    event_types = [e["event_type"] for e in events]
    assert "receivable_diagnosis_failed" in event_types
    assert "receivable_gate_decision" not in event_types


def test_injected_diagnosis_parse_failure_exercises_the_real_failure_path():
    invoice = _base_invoice()
    result, events = _run(invoice, inject_failure="diagnosis_parse_failure")
    assert result["diagnosed_case_reason"] is None
    assert result["diagnosis_matched_ground_truth"] is False
    event_types = [e["event_type"] for e in events]
    assert "receivable_diagnosis_failed" in event_types
    assert "receivable_gate_decision" not in event_types


def test_reminder_cap_escalates_even_with_a_correctly_diagnosed_reason():
    invoice = _base_invoice(reminders_sent_count=MAX_REMINDERS_BEFORE_ESCALATION)
    result, events = _run(
        invoice, diagnosis_return={"case_reason": "payment_process_friction", "reasoning": "matches"},
    )
    assert result["diagnosis_matched_ground_truth"] is True  # diagnosis was RIGHT
    assert result["final_action"] == "no_action_already_escalated"  # but the gate still escalates

    tool_calls = [e for e in events if e["event_type"] == "mcp_tool_call"]
    assert tool_calls[0]["tool"] == "flag_for_manual_review"


def test_stale_invoice_escalates_to_legal_review_even_with_a_correctly_diagnosed_reason():
    invoice = _base_invoice(days_overdue=DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD)
    result, events = _run(
        invoice, diagnosis_return={"case_reason": "payment_process_friction", "reasoning": "matches"},
    )
    assert result["diagnosis_matched_ground_truth"] is True
    assert result["final_action"] == "no_action_stale_invoice_needs_legal_review"

    tool_calls = [e for e in events if e["event_type"] == "mcp_tool_call"]
    assert tool_calls[0]["tool"] == "flag_for_manual_review"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
