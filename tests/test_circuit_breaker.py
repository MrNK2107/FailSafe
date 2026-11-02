"""
recovery_pipeline's LLM-fallback circuit breaker: after CIRCUIT_THRESHOLD
consecutive unusable LLM proposals, every later record is short-circuited
(never proposed, never executed) instead of continuing to hammer a broken
model. A usable proposal in between resets the streak. Previously only the
"reset between runs" side was tested, never the trip itself.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from failsafe import agent, mcp_server, recovery_pipeline
from failsafe.recovery_pipeline import CIRCUIT_THRESHOLD


def _sub(i):
    return {
        "subscription_id": f"sub_cb{i}",
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
    yield paths
    for p in reversed(patches):
        p.stop()


def _run(iso, n, proposals):
    iso["DATA_PATH"].write_text(json.dumps([_sub(i) for i in range(n)]), encoding="utf-8")
    proposer = MagicMock(side_effect=proposals)
    recovery_pipeline.set_proposer(proposer)
    asyncio.run(agent.run(skip_healthcheck=True))
    checkpoint = [
        json.loads(line)
        for line in iso["CHECKPOINT_PATH"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    events = [
        json.loads(line)
        for line in iso["AUDIT_PATH"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return checkpoint, events, proposer


NONE = {"action": None, "reasoning": "no tool call"}
OK = {"action": "delayed_retry", "reasoning": "ok"}


def test_circuit_trips_after_threshold_and_short_circuits_the_rest(iso):
    n = CIRCUIT_THRESHOLD + 3
    checkpoint, events, proposer = _run(iso, n, [NONE] * CIRCUIT_THRESHOLD + [OK] * 10)

    # The model is never called again once the circuit is open.
    assert proposer.call_count == CIRCUIT_THRESHOLD
    tripped = checkpoint[CIRCUIT_THRESHOLD:]
    assert len(tripped) == 3
    for r in tripped:
        assert r["circuit_open"] is True
        assert r["gate_executed"] is False
        assert r["final_action"] == "no_action_unrecoverable"
        assert r["tool_result"] is None  # no MCP tool call at all

    types = [e["event_type"] for e in events]
    assert types.count("circuit_open") == 1
    assert types.count("circuit_open_short_circuit") == 3
    # The pre-trip records still got their POLICY action (the gate, not the
    # model, decides); the tripped ones got no tool call of any kind.
    money_calls = [
        e["subscription_id"]
        for e in events
        if e["event_type"] == "mcp_tool_call" and e.get("tool") == "create_retry_order"
    ]
    assert money_calls == [f"sub_cb{i}" for i in range(CIRCUIT_THRESHOLD)]


def test_a_usable_proposal_resets_the_streak(iso):
    almost = CIRCUIT_THRESHOLD - 1
    proposals = [NONE] * almost + [OK] + [NONE] * almost + [OK]
    checkpoint, events, proposer = _run(iso, len(proposals), proposals)

    assert proposer.call_count == len(proposals)
    assert not any(r.get("circuit_open") for r in checkpoint)
    assert "circuit_open" not in [e["event_type"] for e in events]
    assert all(r["final_action"] == "delayed_retry" and r["gate_executed"] for r in checkpoint)


def test_parse_failure_falls_back_to_policy_and_is_audited(iso):
    checkpoint, events, _ = _run(iso, 1, [NONE])
    (r,) = checkpoint
    # The LLM is advisory: with no usable proposal the gate still applies the
    # policy row for insufficient_funds, and the failure is audited + counted
    # as a policy mismatch rather than silently treated as agreement.
    assert "llm_parse_failure" in [e["event_type"] for e in events]
    assert r["final_action"] == "delayed_retry"
    assert r["llm_matched_policy"] is False


def test_short_circuited_records_are_retried_on_resume(iso, capsys):
    """Regression: records short-circuited by an open circuit were written to
    the checkpoint like finished ones, so after the model was fixed a resumed
    run skipped them forever - they were never evaluated by the gate, never
    flagged for a human, just silently dropped."""
    n = CIRCUIT_THRESHOLD + 2
    _run(iso, n, [NONE] * CIRCUIT_THRESHOLD)
    capsys.readouterr()

    proposer = MagicMock(return_value=OK)
    recovery_pipeline.set_proposer(proposer)
    asyncio.run(agent.run(skip_healthcheck=True))

    assert f"{CIRCUIT_THRESHOLD} already done (checkpoint), 2 remaining" in capsys.readouterr().out
    assert proposer.call_count == 2
    md = iso["RESULTS_PATH"].read_text(encoding="utf-8")
    assert f"processed: **{n}**" in md  # no double counting of the stale entries
    latest = {}
    for line in iso["CHECKPOINT_PATH"].read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        latest[r["subscription_id"]] = r
    assert all(r["final_action"] == "delayed_retry" for r in latest.values())


BAD = {"action": "definitely_not_a_real_action", "reasoning": "garbage"}


def test_repeated_invalid_actions_trip_the_breaker(iso):
    """Regression: an invalid (non-enum) action used to call _record_success(),
    so a model that kept answering with garbage actions never tripped the
    breaker. It now counts, and the rest of the batch is short-circuited."""
    threshold = recovery_pipeline.INVALID_ACTION_THRESHOLD
    n = threshold + 2
    checkpoint, events, proposer = _run(iso, n, [BAD] * n)

    assert proposer.call_count == threshold
    opened = [e for e in events if e["event_type"] == "circuit_open"]
    assert len(opened) == 1 and opened[0]["cause"] == "consecutive_invalid_actions"
    assert [e["event_type"] for e in events].count("llm_invalid_action") == threshold
    tripped = checkpoint[threshold:]
    assert len(tripped) == 2
    assert all(
        r["circuit_open"] and not r["gate_executed"] and r["tool_result"] is None for r in tripped
    )
    # Pre-trip records still got the gate's POLICY action, never the garbage.
    assert all(r["final_action"] == "delayed_retry" for r in checkpoint[:threshold])


def test_invalid_action_threshold_is_configurable(iso, monkeypatch):
    monkeypatch.setattr(recovery_pipeline, "INVALID_ACTION_THRESHOLD", 2)
    checkpoint, events, proposer = _run(iso, 4, [BAD] * 4)
    assert proposer.call_count == 2
    assert [bool(r.get("circuit_open")) for r in checkpoint] == [False, False, True, True]


def test_mixed_invalid_and_parse_failures_share_the_fallback_streak(iso, monkeypatch):
    # A high invalid-only threshold: the trip must come from the shared streak.
    monkeypatch.setattr(recovery_pipeline, "INVALID_ACTION_THRESHOLD", 100)
    proposals = [BAD, NONE] * CIRCUIT_THRESHOLD
    checkpoint, events, proposer = _run(iso, CIRCUIT_THRESHOLD + 1, proposals)
    assert proposer.call_count == CIRCUIT_THRESHOLD
    opened = [e for e in events if e["event_type"] == "circuit_open"]
    assert len(opened) == 1 and opened[0]["cause"] == "consecutive_fallbacks"
    assert checkpoint[-1]["circuit_open"] is True


def test_a_valid_proposal_resets_the_invalid_streak(iso, monkeypatch):
    monkeypatch.setattr(recovery_pipeline, "INVALID_ACTION_THRESHOLD", 2)
    proposals = [BAD, OK, BAD, OK, BAD]
    checkpoint, events, proposer = _run(iso, len(proposals), proposals)
    assert proposer.call_count == len(proposals)
    assert "circuit_open" not in [e["event_type"] for e in events]


def test_unhashable_action_is_treated_as_invalid(iso, monkeypatch):
    monkeypatch.setattr(recovery_pipeline, "INVALID_ACTION_THRESHOLD", 1)
    checkpoint, events, _ = _run(iso, 2, [{"action": ["delayed_retry"], "reasoning": "list"}])
    assert checkpoint[0]["final_action"] == "delayed_retry"  # gate's policy, not the model
    assert checkpoint[1]["circuit_open"] is True


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, CIRCUIT_THRESHOLD),
        ("3", 3),
        ("0", CIRCUIT_THRESHOLD),
        ("-2", CIRCUIT_THRESHOLD),
        ("abc", CIRCUIT_THRESHOLD),
    ],
)
def test_invalid_action_threshold_env_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("FAILSAFE_INVALID_ACTION_THRESHOLD", raising=False)
    else:
        monkeypatch.setenv("FAILSAFE_INVALID_ACTION_THRESHOLD", raw)
    assert recovery_pipeline._invalid_action_threshold_from_env() == expected
