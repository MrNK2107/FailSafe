"""Decision Trace page (src/failsafe/decision_trace.py -> TRACE.html)."""

import json
import re
from pathlib import Path

import pytest

from failsafe import decision_trace as dt
from failsafe.decline_codes import DECLINE_CODES, load_decline_codes
from test_replay import EXAMPLE, gd

REPO = Path(__file__).parent.parent


def _diag(rid, ok=True):
    return {
        "event_type": "diagnosis",
        "subscription_id": rid,
        "raw_decline_message": "low balance",
        "diagnosed_decline_code": "insufficient_funds",
        "true_decline_code": "insufficient_funds" if ok else "card_expired",
        "diagnosis_matched_ground_truth": ok,
        "diagnosis_reasoning": "r",
        "timestamp": "2026-11-02T01:00:00+00:00",
    }


def _tool(
    rid,
    result='{"id": "order_sim_1", "status": "created", "simulated": true}',
    tool="create_retry_order",
):
    return {
        "event_type": "mcp_tool_call",
        "subscription_id": rid,
        "tool": tool,
        "arguments": {"subscription_id": rid},
        "result": result,
    }


def test_gate_ladder_statuses():
    passed = dt.gate_ladder(gd("s1", amount=12345, halted=3))
    assert [c["status"] for c in passed] == ["pass"] * 6
    assert "INR 123.45" in passed[4]["detail"] and "halted 3 days" in passed[3]["detail"]
    stale = gd(
        "s1",
        reason="Escalated to manual review: halted 14 days ago, at or past the 12-day staleness threshold.",
    )
    stale["llm_matched_policy"] = False
    lad = dt.gate_ladder(stale)
    assert [c["status"] for c in lad] == [
        "override",
        "pass",
        "pass",
        "decided",
        "skipped",
        "skipped",
    ]


def test_policy_action_detail_uses_policy_table_when_overridden():
    e = gd("s1", proposed="immediate_retry")
    e["llm_matched_policy"] = False
    traces = dt.build_traces([e], dict(DECLINE_CODES))
    checks = traces[0]["steps"][0]["checks"]
    assert "policy says delayed_retry" in checks[0]["detail"]
    assert traces[0]["overridden"] is True


def test_build_traces_full_story_and_verdicts():
    ev = [
        {"event_type": "run_started", "run_id": "r1"},
        _diag("s1"),
        gd("s1", run_id="r1"),
        _tool("s1"),
        _diag("s2", ok=False),
        gd(
            "s2",
            "card_expired",
            "payment_link_nudge",
            final="no_action_unrecoverable",
            run_id="r1",
            halted=13,
            reason="Escalated to manual review: halted 13 days ago, at or past the 12-day staleness threshold for an automated nudge.",
        ),
        _tool("s2", '{"flagged": true}', "flag_for_manual_review"),
        gd("s3", run_id="r1"),
        _tool("s3", '{"error": "rate limited"}'),
        {
            "event_type": "mcp_tool_error",
            "subscription_id": "s3",
            "tool": "create_retry_order",
            "error": "rate limited",
        },
        {
            "event_type": "diagnosis_failed",
            "subscription_id": "s4",
            "note": "no tool call",
            "true_decline_code": "card_expired",
        },
        _tool("s4", '{"flagged": true}', "flag_for_manual_review"),
        gd(
            "s5",
            run_id="r1",
            amount=999_999_999_99,
            final="no_action_unrecoverable",
            execute=False,
            reason="Amount 9999999999.99 exceeds per-action cap of 50000.00.",
        ),
        {"event_type": "run_finished"},
    ]
    traces = dt.build_traces(ev, None, {})
    by = {t["record_id"]: t for t in traces}
    assert set(by) == {"s1", "s2", "s3", "s4", "s5"}
    assert by["s1"]["verdict"] == dt.VERDICT_EXECUTED and [
        s["type"] for s in by["s1"]["steps"]
    ] == ["diagnosis", "gate_decision", "mcp_tool_call"]
    assert by["s1"]["steps"][2]["result"]["id"] == "order_sim_1"
    assert by["s1"]["replay"]["status"] == "reproduced"
    assert by["s2"]["verdict"] == dt.VERDICT_ESCALATED and by["s2"]["check"] == "staleness"
    assert by["s2"]["steps"][0]["tone"] == "warn"  # diagnosis missed ground truth
    assert by["s3"]["verdict"] == dt.VERDICT_BLOCKED and len(by["s3"]["steps"]) == 3
    assert (
        by["s4"]["verdict"] == dt.VERDICT_REVIEW
        and by["s4"]["code"] == "card_expired"
        and by["s4"]["replay"]["status"] == "n/a"
    )
    assert by["s5"]["verdict"] == dt.VERDICT_BLOCKED and by["s5"]["check"] == "spend_cap"

    s = dt.summarize(traces)
    assert (s["records"], s["executed"], s["escalated"], s["blocked"], s["review"], s["runs"]) == (
        5,
        1,
        1,
        2,
        1,
        1,
    )
    assert s["collected_paise"] == 10_000 and s["reproduced"] == s["replayed"] == 4


def test_second_decision_for_a_record_in_one_run_is_a_new_trace():
    traces = dt.build_traces(
        [_diag("s1"), gd("s1"), _tool("s1"), _diag("s1"), gd("s1"), _tool("s1")]
    )
    assert len(traces) == 2 and len(dt.latest_per_record(traces)) == 1


def test_dataset_fills_ladder_inputs_for_old_logs():
    traces = dt.build_traces(
        [gd("s1", with_inputs=False)], None, {"s1": {"amount_paise": 4200, "halted_days_ago": 2}}
    )
    checks = traces[0]["steps"][0]["checks"]
    assert traces[0]["amount_paise"] == 4200 and "halted 2 days" in checks[3]["detail"]


def test_step_parses_odd_tool_results_and_other_events():
    assert dt._parse_result({"a": 1}) == {"a": 1}
    assert dt._parse_result("plain") == {"text": "plain"}
    assert dt._parse_result("[1]") == {"text": "[1]"}
    assert dt._parse_result(None) == {}
    st = dt._step(
        {"event_type": "detection_decision", "classification": "needs_attention", "reasoning": "r"}
    )
    assert st["lines"] == ["classified needs_attention", "r"]
    st = dt._step({"event_type": "llm_invalid_action", "raw_action": "refund"})
    assert st["tone"] == "warn" and st["lines"] == ["refund"]


def test_events_without_record_id_are_ignored():
    assert dt.build_traces([{"event_type": "diagnosis"}, {"event_type": "gate_decision"}]) == []


def test_candidate_policy_marks_replay_differs():
    traces = dt.build_traces([gd("s1")], load_decline_codes(EXAMPLE))
    assert (
        traces[0]["replay"]["status"] == "changed"
        and traces[0]["replay"]["action"] == "immediate_retry"
    )


def test_render_html_embeds_data_safely():
    e = gd("s1")
    e["llm_reasoning"] = "</script><script>alert(1)</script> & <b>"
    page = dt.render_html(dt.build_traces([e]), "logs/<x>.jsonl", "config/p.json")
    assert page.count("</script>") == 2  # only the page's own two script tags close
    assert "alert(1)" in page and "<script>alert" not in page
    assert "logs/&lt;x&gt;.jsonl" in page
    data = re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S).group(
        1
    )
    payload = json.loads(data)
    assert payload["traces"][0]["steps"][0]["proposal"]["reasoning"].startswith("</script>")
    assert payload["stats"]["records"] == 1
    for token in ("{{DATA}}", "{{SOURCE}}", "{{POLICY}}"):
        assert token not in page


def test_main_builds_trace_for_committed_log(tmp_path, capsys):
    out = tmp_path / "TRACE.html"
    assert dt.main(["--out", str(out)]) == 0
    msg = capsys.readouterr().out
    assert "150 records" in msg and "326/326" in msg
    page = out.read_text(encoding="utf-8")
    assert "FailSafe" in page and "sub_2c794247ffee4f" in page


def test_main_onetime_log_and_missing_log(tmp_path, capsys):
    out = tmp_path / "t.html"
    assert (
        dt.main(
            [
                "--audit",
                str(REPO / "logs" / "audit_log_onetime.jsonl"),
                "--data",
                str(REPO / "data" / "failed_onetime_payments.json"),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert "55/55" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        dt.main(["--audit", str(tmp_path / "nope.jsonl")])


def test_rel_outside_repo(tmp_path):
    assert dt._rel(tmp_path / "x.jsonl") == "x.jsonl"
