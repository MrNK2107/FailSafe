"""
Unit tests for mcp_server.py's own independent cap/duplicate guard
(_enforce_tool_level_cap) - the defense-in-depth layer added after
README.md §6 flagged "single point of enforcement, not defense-in-depth"
as a real limitation: the MCP tools had no policy check of their own,
only ever running because agent.py chose to call gate.py first.

This does NOT re-implement decline-code policy (these tools never
receive a decline_code - see mcp_server.py's module docstring for why),
only the two checks that need no decision context: spending cap and a
per-run duplicate-call refusal. These tests call the guard directly,
the same way test_gate.py tests Gate.evaluate() directly, rather than
through a real MCP round-trip - fast, and independent of whether
SIMULATE is on (see test_idempotency_integration.py for the version that
goes through a real batch-shaped run instead).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gate import MAX_ACTION_AMOUNT_PAISE, MAX_RUN_TOTAL_PAISE
from mcp_server import (
    ToolLevelCapExceeded,
    _enforce_tool_level_cap,
    _reset_tool_level_guard_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_guard():
    _reset_tool_level_guard_for_tests()
    yield
    _reset_tool_level_guard_for_tests()


def test_guard_allows_a_normal_call():
    _enforce_tool_level_cap("sub_guard_1", "create_retry_order", 29900)  # must not raise


def test_guard_blocks_amount_over_per_action_cap():
    with pytest.raises(ToolLevelCapExceeded, match="per-action cap"):
        _enforce_tool_level_cap("sub_guard_2", "create_retry_order", MAX_ACTION_AMOUNT_PAISE + 1)


def test_guard_blocks_run_total_over_cap():
    # Each call stays under the per-action cap on its own; only the sum
    # crosses the run-total cap - proves the two checks are independent,
    # not the same threshold applied twice.
    per_call = MAX_ACTION_AMOUNT_PAISE - 1
    calls_to_fill = MAX_RUN_TOTAL_PAISE // per_call
    for i in range(calls_to_fill):
        _enforce_tool_level_cap(f"sub_guard_3_{i}", "create_retry_order", per_call)
    with pytest.raises(ToolLevelCapExceeded, match="run-total"):
        _enforce_tool_level_cap("sub_guard_3_last", "create_retry_order", per_call)


def test_guard_blocks_duplicate_subscription_and_tool_pair_in_one_run():
    _enforce_tool_level_cap("sub_guard_4", "create_payment_link", 29900)
    with pytest.raises(ToolLevelCapExceeded, match="duplicate call"):
        _enforce_tool_level_cap("sub_guard_4", "create_payment_link", 29900)


def test_guard_does_not_confuse_different_tools_for_the_same_subscription():
    # A subscription is only ever routed to one tool per decline code in
    # the real pipeline (see gate.py: policy.allowed_action is fixed per
    # decline_code), but the guard keys on (subscription_id, tool_name)
    # specifically, not subscription_id alone - confirms it doesn't
    # over-block a legitimate case it was never meant to catch.
    _enforce_tool_level_cap("sub_guard_5", "create_payment_link", 29900)
    _enforce_tool_level_cap("sub_guard_5", "create_retry_order", 29900)  # must not raise


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
