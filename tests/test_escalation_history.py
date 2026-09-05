"""
Unit tests for agent.py's `_count_prior_attempts` - the pure function that
derives gate.py's cross-run attempt-cap stopping rule input from the audit
log's history. No live Ollama server needed: this only exercises history
counting over a list of plain dicts shaped like real audit log events.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent import _count_prior_attempts


def test_counts_only_matching_subscription_and_attempt_tools():
    events = [
        {"event_type": "mcp_tool_call", "subscription_id": "sub_a", "tool": "create_payment_link"},
        {"event_type": "mcp_tool_call", "subscription_id": "sub_a", "tool": "create_retry_order"},
        {"event_type": "mcp_tool_call", "subscription_id": "sub_b", "tool": "create_payment_link"},
        {"event_type": "gate_decision", "subscription_id": "sub_a"},
    ]
    assert _count_prior_attempts(events, "sub_a") == 2
    assert _count_prior_attempts(events, "sub_b") == 1
    assert _count_prior_attempts(events, "sub_c") == 0


def test_manual_review_escalation_does_not_count_as_an_attempt():
    # Escalating to a human is not itself a retry attempt - counting it
    # would make the attempt cap trigger on its own past escalations.
    events = [
        {"event_type": "mcp_tool_call", "subscription_id": "sub_a", "tool": "flag_for_manual_review"},
    ]
    assert _count_prior_attempts(events, "sub_a") == 0


def test_empty_history_counts_zero():
    assert _count_prior_attempts([], "sub_a") == 0


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
