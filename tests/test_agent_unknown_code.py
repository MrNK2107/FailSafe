"""
Unit tests for the "unknown decline code" safety net in agent.py.

A decline code with no entry in config/decline_policy.json is a
genuinely different failure mode from "the LLM proposed something
wrong" - there is no ground truth to gate against at all. Before this
was handled explicitly, it fell through to get_decline_code()'s
KeyError, caught only by run()'s generic per-record try/except and
logged as an undifferentiated "record_processing_error". These tests
prove the explicit path instead: flagged for manual review, logged with
its own distinct event type, and never reaching the LLM or the gate -
there's nothing for either of them to evaluate.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mcp import Client

from agent import process_one
from audit_log import AuditLogger
from gate import Gate
from mcp_server import server as mcp_server


def _run_process_one(sub: dict, inject_failure: str | None = None):
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        audit_path = Path(f.name)
    audit = AuditLogger(audit_path)
    gate = Gate()

    async def _run():
        async with Client(mcp_server) as client:
            return await process_one(client, gate, audit, sub, inject_failure=inject_failure)

    try:
        result = asyncio.run(_run())
        events = audit.read_all()
        return result, events
    finally:
        audit_path.unlink()


def test_unknown_decline_code_flags_for_manual_review_without_touching_gate():
    sub = {
        "subscription_id": "sub_unknown_test",
        "amount_paise": 10000,
        "decline_code": "this_code_does_not_exist_in_the_policy_table",
    }
    result, events = _run_process_one(sub)

    assert result["final_action"] == "no_action_unrecoverable"
    assert result["gate_executed"] is True
    assert result["llm_matched_policy"] is False

    event_types = [e["event_type"] for e in events]
    assert "unknown_decline_code" in event_types
    # Never reached the gate - there's no policy to gate against.
    assert "gate_decision" not in event_types

    unknown_event = next(e for e in events if e["event_type"] == "unknown_decline_code")
    assert unknown_event["decline_code"] == "this_code_does_not_exist_in_the_policy_table"

    review_call = next(e for e in events if e["event_type"] == "mcp_tool_call")
    assert review_call["tool"] == "flag_for_manual_review"


def test_inject_failure_unknown_decline_code_forces_the_same_path_on_a_real_code():
    # Demo mode: even a perfectly valid, real decline code gets routed to
    # manual review when --inject-failure unknown_decline_code is passed,
    # proving the injection actually exercises the real code path rather
    # than a separate mocked one.
    sub = {
        "subscription_id": "sub_inject_test",
        "amount_paise": 10000,
        "decline_code": "insufficient_funds",
    }
    result, events = _run_process_one(sub, inject_failure="unknown_decline_code")

    assert result["final_action"] == "no_action_unrecoverable"
    event_types = [e["event_type"] for e in events]
    assert "unknown_decline_code" in event_types
    assert "gate_decision" not in event_types


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    if failures:
        raise SystemExit(1)
