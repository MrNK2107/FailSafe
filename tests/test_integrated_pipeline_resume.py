"""
integrated_pipeline.run() paths not covered by test_integrated_pipeline.py:

  * resume from the integrated checkpoint: done records are skipped, each
    domain's gate is seeded, and the pooled run-total is restored into the
    MCP tool-level guard,
  * a malformed record in a data file is logged as a dispatch failure and
    skipped instead of crashing the whole multi-domain run (regression),
  * a per-record crash degrades to that domain's own fallback action,
  * the subscription domain's cross-run attempt cap is fed from that
    domain's real audit history,
  * missing data / corrupted checkpoint lines / round-robin interleaving.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from failsafe import integrated_pipeline as ip
from failsafe import mcp_server, recovery_pipeline
from failsafe.gate import MAX_ATTEMPTS_PER_SUBSCRIPTION


def _sub(i):
    return {
        "subscription_id": f"sub_{i}",
        "amount_paise": 29900,
        "decline_code": "insufficient_funds",
        "raw_decline_message": "insufficient balance",
        "plan": "P",
        "halted_days_ago": 1,
        "simulated_customer_response": True,
    }


def _pay(i):
    return {
        "payment_id": f"pay_{i}",
        "amount_paise": 19900,
        "decline_code": "insufficient_funds",
        "item": "X",
        "simulated_customer_response": False,
    }


def _cart(i):
    return {
        "cart_id": f"cart_{i}",
        "amount_paise": 29900,
        "item": "X",
        "checkout_stage": "otp_entry",
        "minutes_since_abandonment": 3,
        "device_type": "mobile_web",
        "is_returning_customer": True,
        "abandonment_reason": "otp_delay_or_failure",
        "simulated_customer_response": True,
    }


def _inv(i):
    return {
        "invoice_id": f"inv_{i}",
        "amount_paise": 500000,
        "business_name": "B",
        "days_overdue": 5,
        "payment_terms": "net_30",
        "customer_payment_history_signal": "first_time_overdue",
        "reminders_sent_count": 0,
        "last_reminder_response": None,
        "amount_vs_typical_ratio": 1.0,
        "case_reason": "cash_flow_delay",
        "simulated_customer_response": True,
    }


MAKERS = {
    "subscription": _sub,
    "one_time_payment": _pay,
    "checkout_abandonment": _cart,
    "overdue_receivable": _inv,
}


@pytest.fixture
def env(tmp_path):
    originals = {name: dict(cfg) for name, cfg in ip.DOMAINS.items()}
    for name, cfg in ip.DOMAINS.items():
        cfg["data_path"] = tmp_path / f"{name}.json"
        cfg["audit_path"] = tmp_path / f"audit_{name}.jsonl"
    paths = {
        "INTEGRATED_AUDIT_PATH": tmp_path / "audit_integrated.jsonl",
        "INTEGRATED_RESULTS_PATH": tmp_path / "INTEGRATED_RESULTS.md",
        "INTEGRATED_CHECKPOINT_PATH": tmp_path / "integrated_checkpoint.jsonl",
    }
    proposer = MagicMock(return_value={"action": "delayed_retry", "reasoning": "m"})
    patches = [patch.object(ip, k, v) for k, v in paths.items()]
    patches += [
        patch(
            "failsafe.recovery_pipeline.diagnose_decline_code",
            return_value={"decline_code": "insufficient_funds", "reasoning": "m"},
        ),
        patch(
            "failsafe.checkout_abandonment_agent.diagnose_abandonment_reason",
            return_value={"reason": "otp_delay_or_failure", "reasoning": "m"},
        ),
        patch(
            "failsafe.receivables_agent.diagnose_receivable",
            return_value={"case_reason": "cash_flow_delay", "reasoning": "m"},
        ),
    ]
    for p in patches:
        p.start()
    recovery_pipeline.set_proposer(proposer)

    def write(n, extra=None):
        for name, cfg in ip.DOMAINS.items():
            recs = [MAKERS[name](i) for i in range(n)] + (extra or {}).get(name, [])
            cfg["data_path"].write_text(json.dumps(recs), encoding="utf-8")

    yield {"paths": paths, "write": write, "proposer": proposer, "tmp": tmp_path}
    for p in reversed(patches):
        p.stop()
    for name, cfg in ip.DOMAINS.items():
        cfg.clear()
        cfg.update(originals[name])


def _events(path):
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _run(per_domain):
    return asyncio.run(ip.run(per_domain=per_domain, skip_healthcheck=True))


def test_missing_domain_data_exits_naming_the_domain(env):
    with pytest.raises(SystemExit, match="No data found for domain 'subscription'"):
        _run(1)


def test_interleave_is_round_robin_with_uneven_lengths():
    batches = {
        "subscription": [1, 2, 3],
        "one_time_payment": [],
        "checkout_abandonment": [4],
        "overdue_receivable": [5, 6],
    }
    assert ip._interleave(batches) == [1, 4, 5, 2, 6, 3]
    assert ip._interleave({name: [] for name in ip.DOMAINS}) == []


def test_resume_skips_done_records_seeds_gates_and_restores_pooled_total(env, capsys):
    env["write"](1)
    _run(1)
    first_calls = env["proposer"].call_count
    assert first_calls == 2  # subscription + one-time each propose once
    mcp_server._reset_tool_level_guard_for_tests()  # simulate a fresh process

    env["write"](2)
    seeds = {}
    real_makers = {name: cfg["make_gate"] for name, cfg in ip.DOMAINS.items()}
    for name, make in real_makers.items():

        def _make(make=make, name=name):
            g = make()
            orig = g.seed_from_checkpoint

            def _seed(spent, seen):
                seeds[name] = (spent, set(seen))
                return orig(spent, seen)

            g.seed_from_checkpoint = _seed
            return g

        ip.DOMAINS[name]["make_gate"] = _make

    results = _run(2)
    out = capsys.readouterr().out

    assert env["proposer"].call_count == first_calls + 2  # only the new records
    assert "4 already done (checkpoint), 4 remaining" in out
    for name, cfg in ip.DOMAINS.items():
        ids = [r[cfg["id_field"]] for r in results[name]]
        assert sorted(ids) == sorted(MAKERS[name](i)[cfg["id_field"]] for i in range(2))
    assert seeds["subscription"] == (29900, {("sub_0", "delayed_retry")})
    assert seeds["overdue_receivable"][0] == 500000
    # The tool-level guard's pooled total was restored from all executed actions.
    assert "pooled run_total restored" in out
    checkpoint = _events(env["paths"]["INTEGRATED_CHECKPOINT_PATH"])
    assert len(checkpoint) == 8
    assert len({(e["domain"], e["record_id"]) for e in checkpoint}) == 8


def test_pooled_total_is_written_into_tool_level_guard(env):
    env["write"](1)
    _run(1)
    executed = sum(
        e["amount_paise"]
        for e in _events(env["paths"]["INTEGRATED_CHECKPOINT_PATH"])
        if e["gate_executed"]
        and e["final_action"] not in {"no_action_unrecoverable", "no_action_needs_human_review"}
    )
    mcp_server._reset_tool_level_guard_for_tests()
    with patch.object(ip, "_interleave", return_value=[]):
        _run(1)
    assert mcp_server._tool_run_total_paise == executed > 0


def test_malformed_record_is_skipped_not_fatal(env):
    """Regression: the checkpoint filter called identify_domain() on every
    record before the loop's own try/except, so one record with no (or two)
    known ID fields crashed the whole integrated run instead of being logged
    as integrated_dispatch_failed and skipped."""
    bad = {"subscription_id": "sub_bad", "cart_id": "cart_bad", "amount_paise": 100}
    env["write"](1, extra={"subscription": [bad]})
    results = _run(2)

    assert [r["subscription_id"] for r in results["subscription"]] == ["sub_0"]
    events = _events(env["paths"]["INTEGRATED_AUDIT_PATH"])
    failed = [e for e in events if e["event_type"] == "integrated_dispatch_failed"]
    assert len(failed) == 1
    assert failed[0]["record_keys"] == sorted(bad)
    assert sum(len(v) for v in results.values()) == 4


def test_per_record_crash_uses_that_domains_fallback(env):
    env["write"](1)
    boom = MagicMock(side_effect=RuntimeError("receivables exploded"))
    with patch.dict(ip.DOMAINS["overdue_receivable"], {"process_one": boom}):
        results = _run(1)
    inv = results["overdue_receivable"][0]
    assert inv["final_action"] == ip.DOMAINS["overdue_receivable"]["fallback_action"]
    assert inv["gate_executed"] is False
    assert results["subscription"][0]["final_action"] == "delayed_retry"
    errs = [
        e
        for e in _events(ip.DOMAINS["overdue_receivable"]["audit_path"])
        if e["event_type"] == "record_processing_error"
    ]
    assert errs and errs[0]["invoice_id"] == "inv_0"


def test_subscription_attempt_cap_reads_that_domains_audit_history(env):
    env["write"](1)
    history = ip.DOMAINS["subscription"]["audit_path"]
    history.write_text(
        "".join(
            json.dumps(
                {
                    "event_type": "mcp_tool_call",
                    "tool": "create_retry_order",
                    "subscription_id": "sub_0",
                }
            )
            + "\n"
            for _ in range(MAX_ATTEMPTS_PER_SUBSCRIPTION)
        ),
        encoding="utf-8",
    )
    results = _run(1)
    sub = results["subscription"][0]
    # Escalated to a human instead of a 4th automated retry.
    assert sub["final_action"] == "no_action_unrecoverable"
    # Other domains are unaffected by the subscription domain's history.
    assert results["one_time_payment"][0]["final_action"] == "delayed_retry"


def test_corrupted_checkpoint_line_is_skipped(env, capsys):
    env["paths"]["INTEGRATED_CHECKPOINT_PATH"].write_text("{nope\n\n", encoding="utf-8")
    assert ip._load_checkpoint() == []
    assert "skipping corrupted line" in capsys.readouterr().out


def test_healthcheck_failure_aborts(env):
    with patch("failsafe.ollama_client.healthcheck", return_value=(False, "ollama down")):
        with pytest.raises(SystemExit, match="ollama down"):
            asyncio.run(ip.run(per_domain=1))


def test_healthcheck_real_keys_require_docker():
    with (
        patch("failsafe.ollama_client.healthcheck", return_value=(True, "ok")),
        patch("failsafe.razorpay_client.SIMULATE", False),
        patch.object(ip.subprocess, "run", side_effect=OSError("no docker")),
    ):
        with pytest.raises(SystemExit, match="docker not reachable"):
            ip._check_health()


def test_circuit_short_circuited_entries_are_not_treated_as_done(env):
    env["paths"]["INTEGRATED_CHECKPOINT_PATH"].write_text(
        json.dumps({"domain": "subscription", "record_id": "sub_0", "circuit_open": True})
        + "\n"
        + json.dumps(
            {"domain": "subscription", "record_id": "sub_1", "final_action": "delayed_retry"}
        )
        + "\n",
        encoding="utf-8",
    )
    assert [e["record_id"] for e in ip._load_checkpoint()] == ["sub_1"]
