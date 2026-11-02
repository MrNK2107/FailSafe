"""
Corrected override metric. The legacy "LLM proposals the gate had to
override" counts every record with llm_matched_policy falsy - including ones
where the model never proposed a usable action at all (unknown decline code,
parse failure, invalid action, open circuit, crash). The corrected
`override_rate_of_proposals` only divides over records with a real, valid
proposal. Both are reported side by side; the legacy one is unchanged.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from failsafe import agent, agent_onetime, recovery_pipeline
from failsafe.recovery_pipeline import override_metrics, render_corrected_override_line


def _sub(i, code="insufficient_funds"):
    return {
        "subscription_id": f"sub_om{i}",
        "amount_paise": 1000 + i,
        "decline_code": code,
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
        # the single simulate switch (razorpay_client.force_simulate), patcher-style
        patch("failsafe.razorpay_client._forced", [True]),
    ]
    for p in patches:
        p.start()
    yield paths
    for p in reversed(patches):
        p.stop()


def test_metric_unit():
    rows = [
        {"llm_matched_policy": True, "llm_proposed": True},
        {"llm_matched_policy": False, "llm_proposed": True},
        {"llm_matched_policy": False, "llm_proposed": False},  # parse failure etc.
        {"llm_matched_policy": None, "llm_proposed": False},  # left alone by detection
        {"llm_matched_policy": False},  # old checkpoint row
    ]
    m = override_metrics(rows)
    assert (m["overridden"], m["total"]) == (4, 5)  # legacy, unchanged semantics
    assert (m["overridden_proposals"], m["proposals"]) == (1, 2)
    assert m["override_rate_of_proposals"] == 0.5
    assert (m["no_proposal"], m["unknown"]) == (2, 1)
    assert "1 checkpointed records predate this field" in render_corrected_override_line(rows)


def test_metric_empty_and_no_proposals():
    assert override_metrics([])["override_rate"] == 0.0
    m = override_metrics([{"llm_matched_policy": False, "llm_proposed": False}])
    assert m["override_rate_of_proposals"] == 0.0 and m["proposals"] == 0


def test_agent_run_reports_both_metrics(iso):
    subs = [_sub(0), _sub(1), _sub(2), _sub(3), _sub(4, code="totally_unknown_code")]
    iso["DATA_PATH"].write_text(json.dumps(subs), encoding="utf-8")
    proposals = [
        {"action": "delayed_retry", "reasoning": "matches policy"},
        {"action": "immediate_retry", "reasoning": "wrong - gate overrides"},
        {"action": None, "reasoning": "parse failure"},
        {"action": "not_an_action", "reasoning": "invalid"},
    ]
    recovery_pipeline.set_proposer(MagicMock(side_effect=proposals))
    asyncio.run(agent.run(skip_healthcheck=True))

    checkpoint = [
        json.loads(x) for x in iso["CHECKPOINT_PATH"].read_text(encoding="utf-8").splitlines()
    ]
    assert [r["llm_proposed"] for r in checkpoint] == [True, True, False, False, False]
    md = iso["RESULTS_PATH"].read_text(encoding="utf-8")
    # Legacy line: 4/5 - three of those "overrides" never had a proposal.
    assert "LLM proposals the gate had to override (policy mismatch): **4/5**" in md
    assert "actually proposed a valid action: **1/2** (50%; 3 records had no usable proposal" in md


def test_crash_fallback_is_not_a_proposal(iso):
    iso["DATA_PATH"].write_text(json.dumps([_sub(0)]), encoding="utf-8")

    async def boom(*a, **kw):
        raise RuntimeError("crash")

    with patch.object(agent, "process_one", boom):
        asyncio.run(agent.run(skip_healthcheck=True))
    (r,) = [json.loads(x) for x in iso["CHECKPOINT_PATH"].read_text(encoding="utf-8").splitlines()]
    assert r["llm_proposed"] is False
    assert "**0/0** (0%; 1 records had no usable proposal" in iso["RESULTS_PATH"].read_text(
        encoding="utf-8"
    )


def test_agent_onetime_results_include_corrected_line(tmp_path):
    out = tmp_path / "R.md"
    with patch.object(agent_onetime, "RESULTS_PATH", out):
        agent_onetime.write_results(
            [
                {
                    "payment_id": "p",
                    "amount_paise": 1,
                    "decline_code": "x",
                    "final_action": "no_action_unrecoverable",
                    "gate_executed": True,
                    "llm_matched_policy": False,
                    "llm_proposed": False,
                    "simulated_customer_response": False,
                },
            ]
        )
    md = out.read_text(encoding="utf-8")
    assert "override: **1/1**" in md
    assert "Corrected metric" in md and "**0/0**" in md
