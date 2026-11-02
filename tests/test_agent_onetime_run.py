"""
End-to-end tests of agent_onetime.run() (failed one-time payments), which
previously had no coverage beyond process_one's unknown-code path.
LLM proposer injected through recovery_pipeline's seam; gate, policy,
audit log and the in-process MCP server (SIMULATE) are real.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from failsafe import agent_onetime, mcp_server, recovery_pipeline


def _pay(i, code="insufficient_funds", amount=50000):
    return {
        "payment_id": f"pay_t{i}",
        "item": "Thing",
        "amount_paise": amount,
        "currency": "INR",
        "decline_code": code,
        "simulated_customer_response": True,
    }


@pytest.fixture
def iso(tmp_path):
    paths = {
        "DATA_PATH": tmp_path / "payments.json",
        "AUDIT_PATH": tmp_path / "audit.jsonl",
        "CHECKPOINT_PATH": tmp_path / "checkpoint.jsonl",
        "RESULTS_PATH": tmp_path / "RESULTS_ONETIME.md",
    }
    patches = [patch.object(agent_onetime, k, v) for k, v in paths.items()]
    patches += [
        patch.object(mcp_server, "SIMULATE", True),
        patch.object(mcp_server._rp, "simulate", True),
    ]
    for p in patches:
        p.start()
    yield paths
    for p in reversed(patches):
        p.stop()


def _write(iso, pays):
    iso["DATA_PATH"].write_text(json.dumps(pays), encoding="utf-8")


def _checkpoint(iso):
    return [
        json.loads(line)
        for line in iso["CHECKPOINT_PATH"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_missing_data_exits(iso):
    with pytest.raises(SystemExit, match="No data found"):
        asyncio.run(agent_onetime.run())


def test_gate_overrides_a_wrong_llm_proposal_and_results_report_it(iso):
    # insufficient_funds -> policy delayed_retry; card_expired -> payment_link_nudge.
    _write(iso, [_pay(0, "insufficient_funds"), _pay(1, "card_expired")])
    # The "LLM" proposes delayed_retry for everything: wrong for card_expired.
    recovery_pipeline.set_proposer(
        MagicMock(return_value={"action": "delayed_retry", "reasoning": "naive"})
    )
    asyncio.run(agent_onetime.run())

    by_id = {r["payment_id"]: r for r in _checkpoint(iso)}
    assert by_id["pay_t0"]["final_action"] == "delayed_retry"
    assert by_id["pay_t0"]["llm_matched_policy"] is True
    assert by_id["pay_t1"]["final_action"] == "payment_link_nudge"  # policy wins
    assert by_id["pay_t1"]["llm_matched_policy"] is False

    md = iso["RESULTS_PATH"].read_text(encoding="utf-8")
    assert "Total failed one-time payments processed: **2**" in md
    assert "override: **1/2** (50%)" in md
    assert "| card_expired | 1 | payment_link_nudge |" in md


def test_resume_does_not_reexecute_or_double_spend(iso):
    _write(iso, [_pay(0), _pay(1)])
    proposer = MagicMock(return_value={"action": "delayed_retry", "reasoning": "m"})
    recovery_pipeline.set_proposer(proposer)
    asyncio.run(agent_onetime.run())
    assert proposer.call_count == 2

    seed = MagicMock()
    with patch.object(agent_onetime.Gate, "seed_from_checkpoint", seed):
        asyncio.run(agent_onetime.run())
    assert proposer.call_count == 2  # nothing re-proposed
    spent, seen = seed.call_args.args
    assert spent == 100000
    assert seen == {("pay_t0", "delayed_retry"), ("pay_t1", "delayed_retry")}
    assert len(_checkpoint(iso)) == 2


def test_run_resets_a_tripped_circuit_breaker(iso):
    _write(iso, [_pay(0)])
    recovery_pipeline.set_proposer(
        MagicMock(return_value={"action": "delayed_retry", "reasoning": "m"})
    )
    recovery_pipeline._circuit_open = True
    asyncio.run(agent_onetime.run())
    assert _checkpoint(iso)[0]["final_action"] == "delayed_retry"


def test_per_record_crash_is_contained(iso):
    _write(iso, [_pay(0), _pay(1)])
    recovery_pipeline.set_proposer(
        MagicMock(return_value={"action": "delayed_retry", "reasoning": "m"})
    )
    real = agent_onetime.process_one

    async def _flaky(client, gate, audit, pay):
        if pay["payment_id"] == "pay_t0":
            raise RuntimeError("boom")
        return await real(client, gate, audit, pay)

    with patch.object(agent_onetime, "process_one", _flaky):
        asyncio.run(agent_onetime.run())
    first, second = _checkpoint(iso)
    assert first["final_action"] == "no_action_unrecoverable" and first["gate_executed"] is False
    assert second["final_action"] == "delayed_retry"


def test_corrupted_checkpoint_line_is_skipped(iso, capsys):
    iso["CHECKPOINT_PATH"].write_text('{"payment_id": "x"\n', encoding="utf-8")
    assert agent_onetime._load_checkpoint() == []
    assert "skipping corrupted line" in capsys.readouterr().out


def test_empty_batch_does_not_crash(iso):
    """Regression: write_results divided by len(results)."""
    _write(iso, [])
    asyncio.run(agent_onetime.run())
    assert "No records processed" in iso["RESULTS_PATH"].read_text(encoding="utf-8")


def test_circuit_short_circuited_entries_are_not_treated_as_done(iso):
    iso["CHECKPOINT_PATH"].write_text(
        json.dumps(
            {
                "payment_id": "pay_t0",
                "final_action": "no_action_unrecoverable",
                "circuit_open": True,
            }
        )
        + "\n"
        + json.dumps(
            {"payment_id": "pay_t1", "final_action": "delayed_retry", "gate_executed": True}
        )
        + "\n",
        encoding="utf-8",
    )
    assert [r["payment_id"] for r in agent_onetime._load_checkpoint()] == ["pay_t1"]
