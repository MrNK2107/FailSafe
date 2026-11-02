"""
mcp_server's tool-level guard (_tool_seen / _tool_run_total_paise) is
process-global. Regression: agent.run() neither reset it nor restored it from
the checkpoint, so a second in-process run inherited the first run's seen
keys (its first retry of the same subscription was refused as a "duplicate"
by the tool) and run total, while a resumed run started with an empty guard.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from failsafe import agent, agent_onetime, mcp_server, recovery_pipeline

OK = {"action": "delayed_retry", "reasoning": "ok"}


def _sub(i):
    return {
        "subscription_id": f"sub_tg{i}",
        "amount_paise": 1000 + i,
        "decline_code": "insufficient_funds",
        "plan": "P",
        "halted_days_ago": 1,
        "simulated_customer_response": True,
    }


@pytest.fixture
def iso(tmp_path):
    paths = {
        "DATA_PATH": tmp_path / "subs.json",
        "AUDIT_PATH": tmp_path / "audit.jsonl",
        "CHECKPOINT_PATH": tmp_path / "checkpoint.jsonl",
        "RESULTS_PATH": tmp_path / "RESULTS.md",
    }
    patches = [patch.object(agent, k, v) for k, v in paths.items()]
    patches += [
        patch.object(mcp_server, "SIMULATE", True),
        patch.object(mcp_server._rp, "simulate", True),
    ]
    for p in patches:
        p.start()
    recovery_pipeline.set_proposer(MagicMock(return_value=OK))
    yield paths
    for p in reversed(patches):
        p.stop()


def _checkpoint(iso):
    return [
        json.loads(x)
        for x in iso["CHECKPOINT_PATH"].read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]


def test_stale_guard_state_from_a_previous_run_does_not_leak(iso):
    iso["DATA_PATH"].write_text(json.dumps([_sub(0), _sub(1)]), encoding="utf-8")
    # Simulate a previous in-process run that already retried sub_tg0 and
    # pushed the run total to the cap.
    mcp_server.seed_tool_level_guard(
        mcp_server.MAX_RUN_TOTAL_PAISE, {("sub_tg0", "create_retry_order")}
    )

    asyncio.run(agent.run(skip_healthcheck=True))

    results = _checkpoint(iso)
    assert [r["final_action"] for r in results] == ["delayed_retry", "delayed_retry"]
    # Without the reset the tool refuses both calls (duplicate / cap) - the
    # tool result would be an error rather than a created order.
    # (an MCP tool error surfaces as "Error executing tool ...").
    assert all("Error executing tool" not in str(r["tool_result"]) for r in results)
    total, seen = mcp_server.tool_level_guard_state()
    assert total == 1000 + 1001
    assert seen == {("sub_tg0", "create_retry_order"), ("sub_tg1", "create_retry_order")}


def test_two_runs_in_one_process_start_from_a_clean_guard(iso):
    iso["DATA_PATH"].write_text(json.dumps([_sub(0)]), encoding="utf-8")
    asyncio.run(agent.run(skip_healthcheck=True))
    # Fresh run of the same data (checkpoint removed, e.g. a new batch day).
    iso["CHECKPOINT_PATH"].unlink()
    asyncio.run(agent.run(skip_healthcheck=True))
    (r,) = _checkpoint(iso)
    assert r["gate_executed"] and "Error executing tool" not in str(r["tool_result"])
    assert mcp_server.tool_level_guard_state() == (
        1000,
        frozenset({("sub_tg0", "create_retry_order")}),
    )


def test_resumed_run_restores_the_guard_from_the_checkpoint(iso):
    iso["DATA_PATH"].write_text(json.dumps([_sub(0), _sub(1)]), encoding="utf-8")
    done = {
        **_sub(0),
        "final_action": "delayed_retry",
        "gate_executed": True,
        "llm_matched_policy": True,
        "tool_result": "x",
    }
    flagged = {
        **_sub(1),
        "subscription_id": "sub_flag",
        "final_action": "no_action_fraud",
        "gate_executed": True,
        "llm_matched_policy": True,
        "tool_result": "x",
    }
    iso["CHECKPOINT_PATH"].write_text(
        json.dumps(done) + "\n" + json.dumps(flagged) + "\n", encoding="utf-8"
    )
    seen_during_run = {}

    real_process = agent.process_one

    async def spy(*a, **kw):
        seen_during_run["state"] = mcp_server.tool_level_guard_state()
        return await real_process(*a, **kw)

    with patch.object(agent, "process_one", spy):
        asyncio.run(agent.run(skip_healthcheck=True))

    total, seen = seen_during_run["state"]
    assert total == 1000  # only the executed money-moving action counts
    assert seen == {("sub_tg0", "create_retry_order")}  # manual-review flags are not guarded


def test_tool_keys_from_checkpoint_skips_unknown_and_unexecuted():
    rows = [
        {"subscription_id": "a", "final_action": "payment_link_nudge", "gate_executed": True},
        {"subscription_id": "b", "final_action": "delayed_retry", "gate_executed": False},
        {"subscription_id": "c", "final_action": "left_alone_by_detection", "gate_executed": True},
        {"subscription_id": "d", "final_action": "no_action_unrecoverable", "gate_executed": True},
    ]
    assert agent._tool_keys_from_checkpoint(rows) == {("a", "create_payment_link")}


def test_agent_onetime_run_resets_the_guard(tmp_path):
    payments = [
        {
            "payment_id": "pay_tg0",
            "amount_paise": 2000,
            "decline_code": "insufficient_funds",
            "item": "I",
            "simulated_customer_response": True,
        }
    ]
    data = tmp_path / "p.json"
    data.write_text(json.dumps(payments), encoding="utf-8")
    mcp_server.seed_tool_level_guard(
        mcp_server.MAX_RUN_TOTAL_PAISE, {("pay_tg0", "create_retry_order")}
    )
    recovery_pipeline.set_proposer(MagicMock(return_value=OK))
    with (
        patch.object(agent_onetime, "DATA_PATH", data),
        patch.object(agent_onetime, "AUDIT_PATH", tmp_path / "a.jsonl"),
        patch.object(agent_onetime, "CHECKPOINT_PATH", tmp_path / "c.jsonl"),
        patch.object(agent_onetime, "RESULTS_PATH", tmp_path / "R.md"),
        patch.object(mcp_server, "SIMULATE", True),
        patch.object(mcp_server._rp, "simulate", True),
    ):
        asyncio.run(agent_onetime.run())
    assert mcp_server.tool_level_guard_state() == (
        2000,
        frozenset({("pay_tg0", "create_retry_order")}),
    )
