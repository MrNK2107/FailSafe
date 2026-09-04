"""
Regression test for a real improvement that fell out of extracting
recovery_pipeline.py (BUILD_LOG.md §12): agent_onetime.py previously had no
"unknown decline code" safety net at all (unlike agent.py) - an unrecognized
code would fall through to get_decline_code()'s KeyError, caught only by
run()'s generic per-record try/except. Now that both agent.py and
agent_onetime.py call the same recovery_pipeline.process_record, this path
is shared. Mirrors tests/test_agent_unknown_code.py's structure.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mcp import Client

from agent_onetime import process_one
from audit_log import AuditLogger
from gate import Gate
from mcp_server import server as mcp_server


def test_onetime_unknown_decline_code_flags_for_manual_review_without_touching_gate():
    pay = {
        "payment_id": "pay_unknown_test",
        "amount_paise": 10000,
        "decline_code": "this_code_does_not_exist_in_the_policy_table",
    }
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        audit_path = Path(f.name)
    audit = AuditLogger(audit_path)
    gate = Gate()

    async def _run():
        async with Client(mcp_server) as client:
            return await process_one(client, gate, audit, pay)

    try:
        result = asyncio.run(_run())
        events = audit.read_all()
    finally:
        audit_path.unlink()

    assert result["final_action"] == "no_action_unrecoverable"
    assert result["gate_executed"] is True
    assert result["payment_id"] == "pay_unknown_test"

    event_types = [e["event_type"] for e in events]
    assert "unknown_decline_code" in event_types
    assert "gate_decision" not in event_types

    unknown_event = next(e for e in events if e["event_type"] == "unknown_decline_code")
    assert unknown_event["payment_id"] == "pay_unknown_test"


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
