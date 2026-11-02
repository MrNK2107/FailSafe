"""Replay engine (src/failsafe/replay.py) and the gate hooks it relies on
(Gate(policy=...), GATE_CHECKS / deciding_check, load_decline_codes)."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from failsafe import replay as rp
from failsafe.decline_codes import DECLINE_CODES, RecoveryAction, load_decline_codes
from failsafe.gate import (
    GATE_CHECKS,
    MAX_ATTEMPTS_PER_SUBSCRIPTION,
    STALE_HALT_ESCALATION_DAYS,
    Gate,
    deciding_check,
)

REPO = Path(__file__).parent.parent
EXAMPLE = REPO / "examples" / "policy_whatif_retry_low_balance.json"


def gd(
    record_id,
    code="insufficient_funds",
    proposed="delayed_retry",
    *,
    amount=10_000,
    prior=0,
    halted=None,
    final=None,
    execute=True,
    reason="Passed spending cap and idempotency checks.",
    run_id=None,
    id_field="subscription_id",
    with_inputs=True,
):
    """A gate_decision event as recovery_pipeline writes it."""
    e = {
        "event_type": "gate_decision",
        "timestamp": "2026-11-02T00:00:00+00:00",
        id_field: record_id,
        "decline_code": code,
        "llm_proposed_action": proposed,
        "llm_reasoning": "because",
        "llm_matched_policy": True,
        "gate_execute": execute,
        "gate_reason": reason,
        "final_action": final or DECLINE_CODES[code].allowed_action.value,
        "prior_attempt_count": prior,
    }
    if with_inputs:
        e["amount_paise"] = amount
        e["halted_days_ago"] = halted
    if run_id:
        e["run_id"] = run_id
    return e


# ---------------------------------------------------------------- gate hooks


def test_deciding_check_names_every_gate_branch():
    g = Gate()
    assert (
        deciding_check(
            g.evaluate(
                "s1", "payment_risk_check_failed", RecoveryAction.NO_ACTION_FRAUD, 100
            ).reason
        )
        == "no_action_policy"
    )
    assert (
        deciding_check(
            g.evaluate(
                "s2",
                "insufficient_funds",
                RecoveryAction.DELAYED_RETRY,
                100,
                prior_attempt_count=MAX_ATTEMPTS_PER_SUBSCRIPTION,
            ).reason
        )
        == "attempt_cap"
    )
    assert (
        deciding_check(
            g.evaluate(
                "s3",
                "insufficient_funds",
                RecoveryAction.DELAYED_RETRY,
                100,
                halted_days_ago=STALE_HALT_ESCALATION_DAYS,
            ).reason
        )
        == "staleness"
    )
    assert (
        deciding_check(
            g.evaluate("s4", "insufficient_funds", RecoveryAction.DELAYED_RETRY, 10**12).reason
        )
        == "spend_cap"
    )
    assert (
        deciding_check(
            g.evaluate("s5", "insufficient_funds", RecoveryAction.DELAYED_RETRY, 100).reason
        )
        == "passed"
    )
    assert (
        deciding_check(
            g.evaluate("s5", "insufficient_funds", RecoveryAction.DELAYED_RETRY, 100).reason
        )
        == "idempotency"
    )
    g._run_total_paise = 10**12
    assert (
        deciding_check(
            g.evaluate("s6", "insufficient_funds", RecoveryAction.DELAYED_RETRY, 100).reason
        )
        == "spend_cap"
    )
    assert [n for n, _ in GATE_CHECKS] == [
        "policy",
        "no_action_policy",
        "attempt_cap",
        "staleness",
        "spend_cap",
        "idempotency",
    ]


def test_gate_uses_injected_policy_table():
    table = dict(DECLINE_CODES)
    table["insufficient_funds"] = replace(
        table["insufficient_funds"], allowed_action=RecoveryAction.IMMEDIATE_RETRY
    )
    d = Gate(policy=table).evaluate("s", "insufficient_funds", RecoveryAction.DELAYED_RETRY, 100)
    assert d.final_action == RecoveryAction.IMMEDIATE_RETRY and not d.llm_matched_policy
    # the live table is untouched
    assert (
        Gate().evaluate("s", "insufficient_funds", RecoveryAction.DELAYED_RETRY, 100).final_action
        == RecoveryAction.DELAYED_RETRY
    )
    with pytest.raises(KeyError):
        Gate(policy={}).evaluate("s", "insufficient_funds", RecoveryAction.DELAYED_RETRY, 100)


def test_load_decline_codes_validates_like_the_live_loader(tmp_path):
    assert (
        load_decline_codes(EXAMPLE)["insufficient_funds"].allowed_action
        == RecoveryAction.IMMEDIATE_RETRY
    )
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "x": {
                    "description": "d",
                    "source": "bank",
                    "allowed_action": "refund_everyone",
                    "simulated_success_rate": 0.1,
                }
            }
        )
    )
    with pytest.raises(ValueError, match="invalid allowed_action"):
        load_decline_codes(bad)


# ---------------------------------------------------------------- loading


def test_load_events_skips_blank_and_torn_lines(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_text('{"event_type": "run_started"}\n\n{"event_type": "gate_dec', encoding="utf-8")
    assert rp.load_events(p) == [{"event_type": "run_started"}]


def test_load_dataset_index(tmp_path):
    assert rp.load_dataset_index(None) == {}
    assert rp.load_dataset_index(tmp_path / "missing.json") == {}
    p = tmp_path / "d.json"
    p.write_text(
        json.dumps(
            [{"subscription_id": "s1", "amount_paise": 5}, {"payment_id": "p1"}, {"other": 1}]
        )
    )
    idx = rp.load_dataset_index(p)
    assert set(idx) == {"s1", "p1"}


def test_record_id_of():
    assert rp.record_id_of({"subscription_id": "s"}) == ("subscription_id", "s")
    assert rp.record_id_of({"payment_id": "p"}) == ("payment_id", "p")
    assert rp.record_id_of({"cart_id": "c"}) is None


def test_segment_runs_by_marker_and_by_run_id():
    ev = [
        {"event_type": "x"},
        {"event_type": "run_started"},
        {"event_type": "y"},
        {"event_type": "z", "run_id": "a"},
        {"event_type": "z", "run_id": "a"},
        {"event_type": "z", "run_id": "b"},
    ]
    runs = rp.segment_runs(ev)
    # x | run_started y | a a (a run_id appears: new process) | b
    assert [len(r) for r in runs] == [1, 2, 2, 1]
    assert rp.segment_runs([]) == []


# ---------------------------------------------------------------- replay


def test_replay_reproduces_and_resets_state_per_run():
    ev = [gd("s1", run_id="r1"), gd("s1", run_id="r2")]  # same record, two runs: not a duplicate
    rep = rp.replay(ev)
    assert [o.status for o in rep.outcomes] == [rp.REPRODUCED, rp.REPRODUCED]
    assert rep.outcomes[1].run_index == 1


def test_replay_reproduces_a_recorded_duplicate():
    dup = gd(
        "s1",
        final="no_action_unrecoverable",
        execute=False,
        reason="Duplicate action for s1 - already processed this run.",
    )
    rep = rp.replay([gd("s1"), dup])
    assert [o.status for o in rep.outcomes] == [rp.REPRODUCED, rp.REPRODUCED]
    assert rep.outcomes[1].replay_check == "idempotency"


def test_replay_detects_an_unmarked_restart():
    # Two decisions for one record in one segment, neither a duplicate: the
    # original process restarted, so the replay must use a fresh gate too.
    rep = rp.replay([gd("s1"), gd("s1")])
    assert [o.status for o in rep.outcomes] == [rp.REPRODUCED, rp.REPRODUCED]


def test_replay_fills_missing_inputs_from_the_dataset():
    e = gd(
        "s1",
        with_inputs=False,
        final="no_action_unrecoverable",
        reason="Escalated to manual review: halted 20 days ago, at or past the 12-day staleness threshold for an automated nudge.",
    )
    rep = rp.replay([e], dataset={"s1": {"amount_paise": 500, "halted_days_ago": 20}})
    o = rep.outcomes[0]
    assert (o.status, o.amount_paise, o.halted_days_ago, o.replay_check) == (
        rp.REPRODUCED,
        500,
        20,
        "staleness",
    )


def test_replay_marks_unreplayable_without_amount():
    rep = rp.replay([gd("s1", with_inputs=False), {"event_type": "gate_decision"}])
    assert len(rep.outcomes) == 1 and rep.outcomes[0].status == rp.UNREPLAYABLE
    assert "--data" in rep.outcomes[0].replay_reason
    assert rep.unreplayable and not rep.changed


def test_replay_candidate_policy_flips_and_counts_money():
    table = load_decline_codes(EXAMPLE)
    ev = [gd("s1", amount=1000), gd("s2", "card_declined", "payment_link_nudge", amount=2000)]
    rep = rp.replay(ev, table)
    assert [o.status for o in rep.changed] == [rp.CHANGED, rp.CHANGED]
    assert rep.transitions()[("delayed_retry", "immediate_retry")] == 1
    assert rep.money_delta_paise() == (0, 2000)
    assert rep.rerouted_paise() == (1, 1000)
    assert rep.llm_agreement() == (0, 2)


def test_replay_money_gained_when_policy_starts_collecting():
    table = dict(DECLINE_CODES)
    table["payment_cancelled"] = replace(
        table["payment_cancelled"], allowed_action=RecoveryAction.PAYMENT_LINK_NUDGE
    )
    e = gd(
        "s1",
        "payment_cancelled",
        "no_action_unrecoverable",
        amount=700,
        reason="No-action policy for 'payment_cancelled' (customer source).",
    )
    rep = rp.replay([e], table)
    assert rep.money_delta_paise() == (700, 0)


def test_replay_code_dropped_from_candidate_goes_to_review():
    table = {k: v for k, v in DECLINE_CODES.items() if k != "insufficient_funds"}
    o = rp.replay([gd("s1")], table).outcomes[0]
    assert o.status == rp.CHANGED and o.replay_action == "no_action_unrecoverable"
    assert "No policy entry" in o.replay_reason


def test_replay_treats_off_enum_proposal_as_safest_fallback():
    e = gd("s1", proposed="refund_everything")
    e["llm_matched_policy"] = False
    assert rp.replay([e]).outcomes[0].status == rp.REPRODUCED


def test_committed_audit_logs_reproduce_exactly():
    """The product claim: every recorded gate decision in the committed logs is
    re-derived bit-for-bit by today's gate and policy."""
    for log, data in [
        ("audit_log.jsonl", "halted_subscriptions.json"),
        ("audit_log_onetime.jsonl", "failed_onetime_payments.json"),
    ]:
        rep = rp.replay(
            rp.load_events(REPO / "logs" / log), None, rp.load_dataset_index(REPO / "data" / data)
        )
        assert rep.outcomes and not rep.changed and not rep.unreplayable, log


# ---------------------------------------------------------------- rendering


def test_render_summary_no_changes():
    rep = rp.replay([gd("s1")], policy_path="p.json", audit_path="a.jsonl")
    md = rp.render_summary(rep)
    assert "| Reproduced exactly | 1 |" in md and "No decision changes" in md and "1/1" in md


def test_render_summary_with_changes_and_limit():
    table = load_decline_codes(EXAMPLE)
    rep = rp.replay([gd(f"s{i}") for i in range(4)], table)
    md = rp.render_summary(rep, title="T", limit=2)
    assert md.startswith("## T") and "| Would change | 4 |" in md
    assert "2 more" in md and "all checks passed" in md and "INR" in md


def test_render_summary_empty_report_has_no_agreement_line():
    md = rp.render_summary(rp.ReplayReport("p", "a"))
    assert "agree" not in md


def test_diff_reports_shows_only_what_the_change_moves():
    ev = [gd("s1"), gd("s2", "card_declined", "payment_link_nudge"), gd("s3", with_inputs=False)]
    base = rp.replay(ev)
    cand = rp.replay(ev, load_decline_codes(EXAMPLE))
    diff = rp.diff_reports(base, cand)
    assert len(diff.changed) == 2 and len(diff.unreplayable) == 1
    same = rp.diff_reports(base, rp.replay(ev))
    assert not same.changed


def test_short_result_variants():
    assert rp._short_result({"a": 1}) == '{"a": 1}'
    assert rp._short_result("not json") == "not json"
    assert rp._short_result('{"id": "x", "junk": 1}') == '{"id": "x"}'
    assert rp._short_result('{"junk": 1}') == '{"junk": 1}'
    assert rp._short_result("[1, 2]") == "[1, 2]"


def test_incident_report_covers_every_step_kind():
    ev = [
        {"event_type": "run_started", "timestamp": "t0"},
        {
            "event_type": "diagnosis",
            "subscription_id": "s1",
            "raw_decline_message": "low balance",
            "diagnosed_decline_code": "insufficient_funds",
            "true_decline_code": "insufficient_funds",
            "diagnosis_matched_ground_truth": True,
        },
        gd("s1"),
        {
            "event_type": "mcp_tool_call",
            "subscription_id": "s1",
            "tool": "create_retry_order",
            "arguments": {"amount_paise": 1},
            "result": '{"id": "order_sim_1", "status": "created"}',
        },
        {"event_type": "llm_parse_failure", "subscription_id": "s1", "note": "no tool call"},
        {"event_type": "mystery", "subscription_id": "s1"},
        {
            "event_type": "diagnosis",
            "subscription_id": "s2",
            "diagnosis_matched_ground_truth": False,
        },
    ]
    rep = rp.replay(ev, policy_path="config/decline_policy.json")
    md = rp.incident_report(ev, "s1", rep)
    for needle in [
        "# Incident report: `s1`",
        "low balance",
        "matches ground truth",
        "LLM proposed `delayed_retry`",
        "Policy table lookup: matched the LLM",
        "Idempotency (once per record per run): pass",
        "order_sim_1",
        "no tool call",
        "**mystery**",
        "**reproduced**",
    ]:
        assert needle in md, needle
    assert "s2" not in md
    assert "No events" in rp.incident_report(ev, "nope")
    assert "## Replay verdict" not in rp.incident_report(ev, "s1")


def test_incident_report_check_ladder_stops_at_deciding_check():
    e = gd(
        "s1",
        final="no_action_unrecoverable",
        prior=3,
        reason="Escalated to manual review: 3 prior recovery attempts already made for s1 across previous runs (compliant-escalation cap: max 3).",
    )
    e["llm_matched_policy"] = False
    md = rp.incident_report([e], "s1", rp.replay([e]))
    assert "overrode the LLM" in md and "**decided**" in md and "not reached" in md


def test_incident_report_changed_and_unreplayable_verdicts():
    ev = [gd("s1"), gd("s2", with_inputs=False)]
    rep = rp.replay(ev, load_decline_codes(EXAMPLE))
    assert "**would change**" in rp.incident_report(ev, "s1", rep)
    assert "not replayable" in rp.incident_report(ev, "s2", rep)


# ---------------------------------------------------------------- CLI


def _log(tmp_path, events):
    p = tmp_path / "audit.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return p


def test_main_determinism_check_on_committed_log(capsys):
    assert rp.main([]) == 0
    out = capsys.readouterr().out
    assert " 0 would change, 0 not replayable" in out and "No decision changes" in out
    assert rp.main(["--format", "md"]) == 0
    assert "| Would change | 0 |" in capsys.readouterr().out


def test_render_text_with_changes():
    rep = rp.replay(
        [gd(f"s{i}") for i in range(3)] + [gd("c", "card_declined", "payment_link_nudge")],
        load_decline_codes(EXAMPLE),
    )
    txt = rp.render_text(rep, limit=2)
    assert "4 would change" in txt and "First 2 of 4 changed records" in txt
    assert (
        "delayed_retry            -> immediate_retry" in txt
        and "INR" in txt
        and "LLM agreement" in txt
    )
    assert "LLM agreement" not in rp.render_text(rp.ReplayReport("p", "a"))


def test_main_fail_on_change_and_summary_file(tmp_path, capsys):
    log = _log(tmp_path, [gd("s1")])
    summary = tmp_path / "summary.md"
    summary.write_text("existing\n", encoding="utf-8")
    assert (
        rp.main(
            [
                "--audit",
                str(log),
                "--policy",
                str(EXAMPLE),
                "--fail-on-change",
                "--summary-md",
                str(summary),
            ]
        )
        == 1
    )
    assert (
        rp.main(
            ["--audit", str(log), "--policy", str(EXAMPLE), "--data", str(tmp_path / "none.json")]
        )
        == 0
    )
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("existing\n") and "Would change | 1" in text
    capsys.readouterr()


def test_main_baseline_what_if(tmp_path, capsys):
    log = _log(tmp_path, [gd("s1")])
    assert (
        rp.main(
            ["--audit", str(log), "--policy", str(EXAMPLE), "--baseline", str(rp.DEFAULT_POLICY)]
        )
        == 0
    )
    assert "what-if" in capsys.readouterr().out


def test_main_record_report(tmp_path, capsys):
    log = _log(tmp_path, [gd("s1")])
    assert rp.main(["--audit", str(log), "--record", "s1"]) == 0
    assert "# Incident report: `s1`" in capsys.readouterr().out


def test_main_errors(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        rp.main(["--audit", str(tmp_path / "missing.jsonl")])
    assert e.value.code == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    log = _log(tmp_path, [gd("s1")])
    assert rp.main(["--audit", str(log), "--policy", str(bad)]) == 2
    assert "policy error" in capsys.readouterr().err


def test_rel_outside_repo(tmp_path):
    assert rp._rel(tmp_path / "x") == str(tmp_path / "x")
    assert rp._rel(rp.DEFAULT_POLICY) == "config/decline_policy.json"


def test_apply_overrides_and_summary_dict():
    table = rp.apply_overrides(DECLINE_CODES, {"insufficient_funds": "immediate_retry"})
    assert table["insufficient_funds"].allowed_action == RecoveryAction.IMMEDIATE_RETRY
    assert DECLINE_CODES["insufficient_funds"].allowed_action == RecoveryAction.DELAYED_RETRY
    with pytest.raises(ValueError, match="unknown decline code"):
        rp.apply_overrides(DECLINE_CODES, {"nope": "immediate_retry"})
    with pytest.raises(ValueError, match="invalid action"):
        rp.apply_overrides(DECLINE_CODES, {"insufficient_funds": "refund"})
    d = rp.summary_dict(rp.replay([gd("s1", amount=300)], table), limit=5)
    assert d["changed"] == 1 and d["rerouted"] == {"records": 1, "paise": 300}
    assert d["changes"][0]["record_id"] == "s1" and d["changes"][0]["run"] == 0
    assert d["transitions"] == [{"from": "delayed_retry", "to": "immediate_retry", "records": 1}]
