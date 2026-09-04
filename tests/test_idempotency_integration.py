"""
Integration test proving the gate's idempotency check actually fires
inside a real batch-shaped run through agent.py's process_one() - not
just Gate.evaluate() called directly in isolation, which
tests/test_gate.py's test_gate_hard_blocks_duplicate_action_same_run
already covers.

README.md §6 used to flag this as a real, honest gap: the synthetic
150-record dataset has no repeated subscription_id by construction, so
RESULTS.md's "hard-blocked: 0" never actually proved the safeguard fires
under real batch conditions - only that it was never asked to. This test
closes that gap directly: two records sharing one subscription_id,
processed through the same shared Gate/AuditLogger/MCP-client sequence
agent.py's run() itself uses for every record in a batch.

Forces simulate mode via a patch regardless of local .env state - a test
run must never place a real Razorpay API call as a side effect of running
the suite, and the same real rzp_test_ keys used for REAL_MCP_RESULTS.md
are commonly present in a local .env.
"""

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mcp import Client

import mcp_server
from agent import process_one
from audit_log import AuditLogger
from gate import Gate


def _run_two_through_one_shared_gate(sub_a: dict, sub_b: dict):
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        audit_path = Path(f.name)
    audit = AuditLogger(audit_path)
    gate = Gate()  # one shared Gate across both records, exactly as run() does across a batch

    async def _run():
        async with Client(mcp_server.server) as client:
            first = await process_one(client, gate, audit, sub_a, inject_failure="llm_parse_failure")
            second = await process_one(client, gate, audit, sub_b, inject_failure="llm_parse_failure")
            return first, second

    try:
        # Patching only the module-level SIMULATE name is NOT sufficient
        # when real rzp_test_ keys are present in .env: mcp_server._rp is
        # a RazorpayClient instance constructed once at mcp_server.py
        # import time, and its own create_payment_link/create_retry_order
        # methods branch on the instance's `self.simulate` (bound at that
        # construction), not on this module-level name - discovered while
        # building the diagnosis feature (BUILD_LOG.md §14) when this
        # exact gap let a live run place a real API call by accident.
        # first["final_action"] below is "delayed_retry" (a real
        # money-moving action, not flag_for_manual_review), so this test
        # was silently placing a real create_retry_order call whenever it
        # ran locally with real keys configured - patched here too so this
        # file's own docstring promise ("a test run must never place a
        # real API call as a side effect") is actually true.
        with patch.object(mcp_server, "SIMULATE", True), \
             patch.object(mcp_server._rp, "simulate", True):
            first, second = asyncio.run(_run())
        events = audit.read_all()
        return first, second, events
    finally:
        audit_path.unlink()
        mcp_server._reset_tool_level_guard_for_tests()


def test_duplicate_subscription_id_in_one_batch_is_hard_blocked_not_double_executed():
    # decline_code, not the LLM's proposal, decides the final action -
    # gate.py's policy always wins (see gate.py's own comment on this) -
    # so inject_failure="llm_parse_failure" here only avoids needing a
    # live Ollama server; it does not change which action gets evaluated.
    sub = {
        "subscription_id": "sub_dup_batch_test",
        "amount_paise": 29900,
        "decline_code": "insufficient_funds",  # policy: delayed_retry (money-moving)
        "simulated_customer_response": False,
    }
    first, second, events = _run_two_through_one_shared_gate(sub, dict(sub))

    assert first["final_action"] == "delayed_retry"
    assert first["gate_executed"] is True

    # Second record: same subscription, same policy-determined action -
    # the gate's idempotency check must hard-block it. This is exactly
    # the condition the real 150-record run never exercised.
    assert second["gate_executed"] is False
    assert second["final_action"] == "no_action_unrecoverable"

    gate_decisions = [e for e in events if e["event_type"] == "gate_decision"]
    assert len(gate_decisions) == 2
    assert gate_decisions[0]["gate_execute"] is True
    assert gate_decisions[1]["gate_execute"] is False
    assert "Duplicate action" in gate_decisions[1]["gate_reason"]

    # The tool actually invoked for the duplicate is flag_for_manual_review,
    # not a second create_retry_order - proves no double-spend happens at
    # the tool-call layer either, not just in the gate's own bookkeeping.
    tool_calls = [e for e in events if e["event_type"] == "mcp_tool_call"]
    assert len(tool_calls) == 2
    assert tool_calls[0]["tool"] == "create_retry_order"
    assert tool_calls[1]["tool"] == "flag_for_manual_review"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
