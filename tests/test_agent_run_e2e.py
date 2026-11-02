"""
End-to-end test of agent.run() - the real batch entry point behind
`make demo` - against a small synthetic batch, with every external
dependency removed:

  * Ollama: propose_action is mocked (no network).
  * Razorpay: mcp_server forced into SIMULATE mode on both the module flag
    and the already-constructed RazorpayClient instance.
  * Filesystem: DATA/AUDIT/CHECKPOINT/RESULTS paths redirected to tmp_path.

Covers the run loop itself (checkpoint resume, RESULTS.md rendering,
circuit-breaker reset between runs), which no other test reaches.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from failsafe import agent, mcp_server, recovery_pipeline


def _subs():
    return [
        {
            "subscription_id": "sub_e2e_1",
            "amount_paise": 29900,
            "decline_code": "insufficient_funds",
            "plan": "Basic",
            "halted_days_ago": 2,
            "simulated_customer_response": True,
        },
        {
            "subscription_id": "sub_e2e_2",
            "amount_paise": 49900,
            "decline_code": "card_expired",
            "plan": "Pro",
            "halted_days_ago": 5,
            "simulated_customer_response": False,
        },
        {
            "subscription_id": "sub_e2e_3",
            "amount_paise": 19900,
            "decline_code": "not_a_real_code",
            "plan": "Basic",
            "halted_days_ago": 1,
            "simulated_customer_response": False,
        },
    ]


@pytest.fixture
def isolated_agent(tmp_path):
    data = tmp_path / "halted_subscriptions.json"
    data.write_text(json.dumps(_subs()), encoding="utf-8")
    paths = {
        "DATA_PATH": data,
        "AUDIT_PATH": tmp_path / "audit.jsonl",
        "CHECKPOINT_PATH": tmp_path / "checkpoint.jsonl",
        "RESULTS_PATH": tmp_path / "RESULTS.md",
    }
    patches = [patch.object(agent, k, v) for k, v in paths.items()]
    patches += [
        patch.object(mcp_server, "SIMULATE", True),
        patch.object(mcp_server._rp, "simulate", True),
        patch.object(
            recovery_pipeline,
            "proposer",
            MagicMock(return_value={"action": "delayed_retry", "reasoning": "mocked"}),
        ),
    ]
    for p in patches:
        p.start()
    yield paths
    for p in reversed(patches):
        p.stop()


def test_run_processes_batch_and_writes_results(isolated_agent):
    asyncio.run(agent.run(skip_healthcheck=True))

    results_md = isolated_agent["RESULTS_PATH"].read_text(encoding="utf-8")
    assert "Total halted subscriptions processed: **3**" in results_md

    checkpoint = [
        json.loads(line)
        for line in isolated_agent["CHECKPOINT_PATH"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_id = {r["subscription_id"]: r for r in checkpoint}
    assert by_id["sub_e2e_1"]["final_action"] == "delayed_retry"
    assert by_id["sub_e2e_1"]["gate_executed"] is True
    assert by_id["sub_e2e_2"]["final_action"] == "payment_link_nudge"
    # Unknown decline code never reaches the LLM/gate policy - flagged for a human.
    assert by_id["sub_e2e_3"]["final_action"] == "no_action_unrecoverable"
    assert "flagged" in by_id["sub_e2e_3"]["tool_result"]

    events = [
        json.loads(line)
        for line in isolated_agent["AUDIT_PATH"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    types = [e["event_type"] for e in events]
    assert types[0] == "run_started"
    assert types[-1] == "run_finished"


def test_rerun_resumes_from_checkpoint_without_reprocessing(isolated_agent, capsys):
    asyncio.run(agent.run(skip_healthcheck=True))
    capsys.readouterr()
    asyncio.run(agent.run(skip_healthcheck=True))
    out = capsys.readouterr().out
    assert "3 already done (checkpoint), 0 remaining" in out

    tool_calls = [
        json.loads(line)
        for line in isolated_agent["AUDIT_PATH"].read_text(encoding="utf-8").splitlines()
        if '"mcp_tool_call"' in line
    ]
    # Second run must not place any new tool calls.
    assert len(tool_calls) == 3


def test_run_resets_a_tripped_circuit_breaker(isolated_agent):
    recovery_pipeline._circuit_open = True
    asyncio.run(agent.run(skip_healthcheck=True))
    checkpoint = isolated_agent["CHECKPOINT_PATH"].read_text(encoding="utf-8")
    assert '"circuit_open": true' not in checkpoint
    assert '"final_action": "delayed_retry"' in checkpoint


def test_run_without_data_exits_cleanly(isolated_agent):
    isolated_agent["DATA_PATH"].unlink()
    with pytest.raises(SystemExit, match="No data found"):
        asyncio.run(agent.run(skip_healthcheck=True))


def test_healthcheck_failure_aborts_before_any_work(isolated_agent):
    with patch("failsafe.ollama_client.healthcheck", return_value=(False, "down")):
        with pytest.raises(SystemExit, match="Healthcheck failed: down"):
            asyncio.run(agent.run())
    assert not isolated_agent["AUDIT_PATH"].exists()


def test_write_results_empty_run(isolated_agent):
    agent.write_results([])
    assert "No records processed" in isolated_agent["RESULTS_PATH"].read_text(encoding="utf-8")
