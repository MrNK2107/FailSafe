"""
agent.run() paths that test_agent_run_e2e.py does not reach:

  * --concurrency > 1 (asyncio.gather + Semaphore): results/checkpoint stay
    in input order, and the deterministic gate's run-total spending cap still
    holds when proposals interleave (the gate check-and-record is synchronous,
    so no two in-flight records can both slip under the cap).
  * --inject-failure only ever affects the FIRST remaining record.
  * a per-record crash degrades to no_action_unrecoverable / not executed,
    in both sequential and concurrent mode, and the batch continues.
  * _check_health: the docker check when real keys are configured.
  * main() wires CLI args through to run().
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from failsafe import agent, base_gate, mcp_server, recovery_pipeline


def _sub(i, code="insufficient_funds", amount=29900):
    return {
        "subscription_id": f"sub_c{i}",
        "amount_paise": amount,
        "decline_code": code,
        "plan": "Basic",
        "halted_days_ago": 2,
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
    yield paths
    for p in reversed(patches):
        p.stop()


def _write(iso, subs):
    iso["DATA_PATH"].write_text(json.dumps(subs), encoding="utf-8")


def _checkpoint(iso):
    return [
        json.loads(line)
        for line in iso["CHECKPOINT_PATH"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def _slow_proposer(*args, **kw):
    # Yield to the event loop so concurrent records genuinely interleave.
    await asyncio.sleep(0)
    return {"action": "delayed_retry", "reasoning": "mocked"}


def test_concurrent_run_matches_sequential_and_preserves_order(iso):
    subs = [_sub(i) for i in range(6)]
    _write(iso, subs)
    recovery_pipeline.set_proposer(_slow_proposer)
    asyncio.run(agent.run(skip_healthcheck=True, concurrency=4))
    conc = _checkpoint(iso)

    iso["CHECKPOINT_PATH"].unlink()
    iso["AUDIT_PATH"].unlink()
    asyncio.run(agent.run(skip_healthcheck=True, concurrency=1))
    seq = _checkpoint(iso)

    assert [r["subscription_id"] for r in conc] == [s["subscription_id"] for s in subs]
    assert all(r["final_action"] == "delayed_retry" and r["gate_executed"] for r in conc)
    assert [(r["subscription_id"], r["final_action"], r["gate_executed"]) for r in conc] == [
        (r["subscription_id"], r["final_action"], r["gate_executed"]) for r in seq
    ]


def test_run_total_spending_cap_holds_under_concurrency(iso):
    # Cap fits exactly 2 of the 5 records' amounts.
    amount = 10_000
    _write(iso, [_sub(i, amount=amount) for i in range(5)])
    recovery_pipeline.set_proposer(_slow_proposer)
    with patch.object(base_gate, "MAX_RUN_TOTAL_PAISE", 2 * amount):
        asyncio.run(agent.run(skip_healthcheck=True, concurrency=5))

    results = _checkpoint(iso)
    executed_spend = sum(
        r["amount_paise"]
        for r in results
        if r["gate_executed"] and r["final_action"] == "delayed_retry"
    )
    assert executed_spend <= 2 * amount
    assert (
        sum(1 for r in results if r["gate_executed"] and r["final_action"] == "delayed_retry") == 2
    )
    # The rest were hard-blocked, never silently executed.
    assert sum(1 for r in results if not r["gate_executed"]) == 3


@pytest.mark.parametrize("concurrency", [1, 3])
def test_inject_failure_only_hits_first_record(iso, concurrency, capsys):
    _write(iso, [_sub(0), _sub(1), _sub(2)])
    recovery_pipeline.set_proposer(
        MagicMock(return_value={"action": "delayed_retry", "reasoning": "m"})
    )
    asyncio.run(
        agent.run(
            inject_failure="unknown_decline_code", skip_healthcheck=True, concurrency=concurrency
        )
    )

    by_id = {r["subscription_id"]: r for r in _checkpoint(iso)}
    assert by_id["sub_c0"]["final_action"] == "no_action_unrecoverable"
    assert by_id["sub_c1"]["final_action"] == "delayed_retry"
    assert by_id["sub_c2"]["final_action"] == "delayed_retry"
    assert capsys.readouterr().out.count("[demo] injecting") == 1


@pytest.mark.parametrize("concurrency", [1, 3])
def test_per_record_crash_is_contained(iso, concurrency):
    _write(iso, [_sub(0), _sub(1)])
    recovery_pipeline.set_proposer(
        MagicMock(return_value={"action": "delayed_retry", "reasoning": "m"})
    )
    real = agent.process_one

    async def _flaky(client, gate, audit, sub, **kw):
        if sub["subscription_id"] == "sub_c0":
            raise RuntimeError("boom")
        return await real(client, gate, audit, sub, **kw)

    with patch.object(agent, "process_one", _flaky):
        asyncio.run(agent.run(skip_healthcheck=True, concurrency=concurrency))

    by_id = {r["subscription_id"]: r for r in _checkpoint(iso)}
    assert by_id["sub_c0"]["final_action"] == "no_action_unrecoverable"
    assert by_id["sub_c0"]["gate_executed"] is False
    assert by_id["sub_c1"]["final_action"] == "delayed_retry"
    errs = [
        json.loads(line)
        for line in iso["AUDIT_PATH"].read_text(encoding="utf-8").splitlines()
        if '"record_processing_error"' in line
    ]
    assert len(errs) == 1 and errs[0]["error"] == "boom"


def test_corrupted_checkpoint_line_is_skipped(iso, capsys):
    _write(iso, [_sub(0)])
    iso["CHECKPOINT_PATH"].write_text("{garbage\n\n", encoding="utf-8")
    recovery_pipeline.set_proposer(
        MagicMock(return_value={"action": "delayed_retry", "reasoning": "m"})
    )
    asyncio.run(agent.run(skip_healthcheck=True))
    assert "skipping corrupted line" in capsys.readouterr().out
    assert [r["subscription_id"] for r in agent._load_checkpoint()] == ["sub_c0"]


class TestCheckHealth:
    def test_skip_does_nothing(self):
        with patch("failsafe.ollama_client.healthcheck") as hc:
            agent._check_health(skip=True)
        hc.assert_not_called()

    def test_simulate_mode_does_not_need_docker(self):
        with (
            patch("failsafe.ollama_client.healthcheck", return_value=(True, "ok")),
            patch("failsafe.razorpay_client.SIMULATE", True),
            patch.object(agent.subprocess, "run") as run,
        ):
            agent._check_health()
        run.assert_not_called()

    def test_real_keys_require_docker(self):
        with (
            patch("failsafe.ollama_client.healthcheck", return_value=(True, "ok")),
            patch("failsafe.razorpay_client.SIMULATE", False),
            patch.object(agent.subprocess, "run", side_effect=FileNotFoundError("docker")),
        ):
            with pytest.raises(SystemExit, match="docker not reachable"):
                agent._check_health()

    def test_real_keys_with_docker_pass(self, capsys):
        with (
            patch("failsafe.ollama_client.healthcheck", return_value=(True, "ok")),
            patch("failsafe.razorpay_client.SIMULATE", False),
            patch.object(agent.subprocess, "run") as run,
        ):
            agent._check_health()
        run.assert_called_once()
        assert "docker reachable" in capsys.readouterr().out


def test_main_passes_cli_args_to_run():
    seen = {}

    async def _fake_run(**kw):
        seen.update(kw)

    with (
        patch(
            "sys.argv",
            [
                "agent",
                "--inject-failure",
                "llm_parse_failure",
                "--skip-healthcheck",
                "--concurrency",
                "3",
            ],
        ),
        patch.object(agent, "run", _fake_run),
    ):
        agent.main()
    assert seen == {
        "inject_failure": "llm_parse_failure",
        "skip_healthcheck": True,
        "concurrency": 3,
    }


def test_main_rejects_unknown_injected_failure():
    with patch("sys.argv", ["agent", "--inject-failure", "not_a_mode"]), pytest.raises(SystemExit):
        agent.main()
