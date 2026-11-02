"""
Regression: an MCP tool that errors (the tool-level guard refuses, or the
tool itself raises) comes back from `Client.call_tool` as a CallToolResult
with `is_error=True` and the text "Error executing tool X". The pipelines
used to store that string as `tool_result` and still mark the record
`gate_executed=True`, so RESULTS*.md counted it as an executed action (and
a simulated recovery). Now such a record is NOT executed, is routed to
`flag_for_manual_review` with a reason, and an `mcp_tool_error` audit event
is logged. A successful call is unaffected.

Runs against the real in-process MCP server in SIMULATE mode; no LLM is
called (proposer and diagnosis are faked).
"""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from mcp import Client

from failsafe import agent, agent_onetime, mcp_server, recovery_pipeline
from failsafe.abandonment_gate import AbandonmentGate
from failsafe.audit_log import AuditLogger
from failsafe.checkout_abandonment_agent import process_one as checkout_process_one
from failsafe.gate import Gate
from failsafe.mcp_tool_errors import TOOL_ERROR_TEXT_PREFIX, tool_call_failed
from failsafe.receivables_agent import process_one as receivables_process_one
from failsafe.receivables_gate import ReceivableGate

OK = {"action": "delayed_retry", "reasoning": "ok"}


@pytest.fixture(autouse=True)
def _sim():
    with mcp_server.simulate_mode(True):
        mcp_server.reset_tool_level_guard()
        previous = recovery_pipeline.set_proposer(MagicMock(return_value=OK))
        recovery_pipeline.reset_circuit_breaker()
        yield
        recovery_pipeline.set_proposer(previous)
        mcp_server.reset_tool_level_guard()


def _audit():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        path = Path(f.name)
    return AuditLogger(path), path


def _sub(sid="sub_te1"):
    return {
        "subscription_id": sid,
        "amount_paise": 1500,
        "decline_code": "insufficient_funds",
        "plan": "Plan",
        "halted_days_ago": 1,
        "simulated_customer_response": True,
    }


def _run_sub(sub, gate=None):
    audit, path = _audit()

    async def go():
        async with Client(mcp_server.server) as client:
            return await agent.process_one(client, gate or Gate(), audit, sub)

    try:
        return asyncio.run(go()), audit.read_all()
    finally:
        path.unlink()


def _types(events):
    return [e["event_type"] for e in events]


# --- detection helper ------------------------------------------------------


def test_structured_is_error_flag_wins_over_text():
    text = SimpleNamespace(text="Error executing tool x")
    assert (
        tool_call_failed(SimpleNamespace(is_error=True, content=[SimpleNamespace(text="fine")]))
        is True
    )
    # A tool that legitimately returns text starting with the prefix is not
    # an error when the server says it isn't.
    assert tool_call_failed(SimpleNamespace(is_error=False, content=[text])) is False


def test_camelcase_is_error_flag_is_honoured():
    assert tool_call_failed(SimpleNamespace(isError=True, content=[])) is True
    assert tool_call_failed(SimpleNamespace(isError=False, content=[])) is False


def test_falls_back_to_text_only_without_a_boolean_flag():
    # MagicMock attributes are not bools -> fall back to the text.
    fake = MagicMock()
    fake.content = [SimpleNamespace(text=f"{TOOL_ERROR_TEXT_PREFIX} create_retry_order")]
    assert tool_call_failed(fake) is True
    fake.content = [SimpleNamespace(text='{"id": "order_1"}')]
    assert tool_call_failed(fake) is False
    assert tool_call_failed(SimpleNamespace(content=[])) is False
    assert tool_call_failed(SimpleNamespace(content=None)) is False


# --- subscription / one-time pipeline (recovery_pipeline.process_record) ---


def test_guard_refusal_is_not_executed_and_goes_to_manual_review():
    # The tool-level guard already saw this (id, tool) pair -> it refuses as
    # a duplicate even though the (fresh) gate lets the action through.
    mcp_server.seed_tool_level_guard(0, {("sub_te1", "create_retry_order")})
    result, events = _run_sub(_sub())

    assert result["final_action"] == "delayed_retry"  # what the gate decided
    assert result["gate_executed"] is False  # but it did NOT happen
    assert result["tool_error"] is True
    assert "create_retry_order" in result["manual_review_reason"]

    err = next(e for e in events if e["event_type"] == "mcp_tool_error")
    assert err["tool"] == "create_retry_order"
    assert err["routed_to"] == "flag_for_manual_review"
    flags = [
        e
        for e in events
        if e["event_type"] == "mcp_tool_call" and e["tool"] == "flag_for_manual_review"
    ]
    assert len(flags) == 1 and '"flagged": true' in flags[0]["result"]


def test_tool_exception_is_not_executed_and_goes_to_manual_review():
    with patch.object(
        mcp_server._rp, "create_retry_order", side_effect=RuntimeError("razorpay down")
    ):
        result, events = _run_sub(_sub("sub_te2"))
    assert result["gate_executed"] is False and result["tool_error"] is True
    assert "mcp_tool_error" in _types(events)
    assert TOOL_ERROR_TEXT_PREFIX in result["tool_result"]


def test_successful_tool_call_is_unaffected():
    result, events = _run_sub(_sub("sub_te3"))
    assert result["gate_executed"] is True
    assert result.get("tool_error", False) is False
    assert "manual_review_reason" not in result
    assert "mcp_tool_error" not in _types(events)
    assert '"status": "created"' in result["tool_result"]


def test_errored_record_is_not_counted_as_acted_on(tmp_path):
    ok, _ = _run_sub(_sub("sub_te4"))
    mcp_server.seed_tool_level_guard(0, {("sub_te5", "create_retry_order")})
    bad, _ = _run_sub(_sub("sub_te5"))
    out = tmp_path / "RESULTS.md"
    with patch.object(agent, "RESULTS_PATH", out):
        agent.write_results([ok, bad])
    text = out.read_text(encoding="utf-8")
    assert "Actions executed (retries/nudges the gate let through): **1**" in text
    assert "(1/1 executed actions" in text
    # A tool error is not a gate block - it is reported on its own line.
    assert "Hard-blocked by gate (spending cap / duplicate): **0**" in text
    assert "MCP tool errors (action NOT executed, routed to manual review): **1**" in text

    out1 = tmp_path / "RESULTS_ONETIME.md"
    with patch.object(agent_onetime, "RESULTS_PATH", out1):
        agent_onetime.write_results([ok, bad])
    t1 = out1.read_text(encoding="utf-8")
    assert "Actions executed (retries/nudges the gate let through): **1**" in t1
    assert "MCP tool errors (action NOT executed, routed to manual review): **1**" in t1


def test_no_tool_error_line_when_there_were_none(tmp_path):
    ok, _ = _run_sub(_sub("sub_te6"))
    out = tmp_path / "RESULTS.md"
    with patch.object(agent, "RESULTS_PATH", out):
        agent.write_results([ok])
    assert "MCP tool errors" not in out.read_text(encoding="utf-8")


def test_failing_manual_review_flag_is_audited():
    # Unknown decline code -> straight to flag_for_manual_review; make that
    # tool itself error. The record was never going to move money, but the
    # failure must be visible in the audit log rather than silently stored.
    sub = {**_sub("sub_te7"), "decline_code": "not_a_real_code"}
    with (
        patch.object(mcp_server, "flag_for_manual_review", side_effect=RuntimeError("boom")),
        patch.object(
            mcp_server.server._tool_manager._tools["flag_for_manual_review"],
            "fn",
            side_effect=RuntimeError("boom"),
        ),
    ):
        result, events = _run_sub(sub)
    assert result["tool_error"] is True
    err = next(e for e in events if e["event_type"] == "mcp_tool_error")
    assert err["tool"] == "flag_for_manual_review" and err["routed_to"] is None


# --- checkout abandonment / receivables (own process_one) ------------------


def _cart(cid="cart_te1"):
    return {
        "cart_id": cid,
        "amount_paise": 150000,
        "item": "Item",
        "checkout_stage": "otp_entry",
        "minutes_since_abandonment": 3,
        "device_type": "mobile_web",
        "is_returning_customer": True,
        "abandonment_reason": "otp_delay_or_failure",
        "simulated_customer_response": True,
    }


def _run_cart(cart):
    audit, path = _audit()

    async def go():
        async with Client(mcp_server.server) as client:
            return await checkout_process_one(client, AbandonmentGate(), audit, cart)

    try:
        with patch(
            "failsafe.checkout_abandonment_agent.diagnose_abandonment_reason",
            return_value={"reason": "otp_delay_or_failure", "reasoning": "x"},
        ):
            return asyncio.run(go()), audit.read_all()
    finally:
        path.unlink()


def test_checkout_guard_refusal_goes_to_manual_review():
    mcp_server.seed_tool_level_guard(0, {("cart_te1", "create_payment_link")})
    result, events = _run_cart(_cart())
    assert result["gate_executed"] is False and result["tool_error"] is True
    assert "mcp_tool_error" in _types(events)
    ok, ok_events = _run_cart(_cart("cart_te2"))
    assert ok["gate_executed"] is True and "mcp_tool_error" not in _types(ok_events)


def _invoice(iid="inv_te1"):
    return {
        "invoice_id": iid,
        "amount_paise": 45000,
        "business_name": "Biz",
        "days_overdue": 5,
        "payment_terms": "net_30",
        "customer_payment_history_signal": "first_time_overdue",
        "reminders_sent_count": 0,
        "last_reminder_response": None,
        "typical_order_amount_paise": 45000,
        "amount_vs_typical_ratio": 1.0,
        "case_reason": "payment_process_friction",
        "simulated_customer_response": True,
    }


def _run_invoice(inv):
    audit, path = _audit()

    async def go():
        async with Client(mcp_server.server) as client:
            return await receivables_process_one(client, ReceivableGate(), audit, inv)

    try:
        with patch(
            "failsafe.receivables_agent.diagnose_receivable",
            return_value={"case_reason": "payment_process_friction", "reasoning": "x"},
        ):
            return asyncio.run(go()), audit.read_all()
    finally:
        path.unlink()


def test_receivables_tool_exception_goes_to_manual_review():
    with patch.object(mcp_server._rp, "create_payment_link", side_effect=RuntimeError("down")):
        result, events = _run_invoice(_invoice())
    assert result["gate_executed"] is False and result["tool_error"] is True
    assert "mcp_tool_error" in _types(events)
    ok, _ = _run_invoice(_invoice("inv_te2"))
    assert ok["gate_executed"] is True and "tool_error" not in ok
