"""
The four standalone live demos (detection_live_demo, diagnosis_live_demo,
real_mcp_demo, route_demo) normally need a running Ollama and/or real
Razorpay test keys, so the suite never exercised them. These tests run each
demo's real run()/write_results() offline: the model stages are replaced
by small deterministic fakes (patched at recovery_pipeline, the same seam
the pipeline tests use), the in-process MCP server is forced into SIMULATE
mode, and every output path is redirected to tmp_path, so the committed
logs/ and *_RESULTS.md files are never touched.

What is checked is the demos' own accounting: false positives/negatives
and the "never attempted" rupee value for detection, the "misdiagnosis
changed the action" count for diagnosis, the sample/log/report of the real
MCP demo, and the cap block plus partner payout for Route.
"""

import asyncio
import json
from unittest.mock import patch

import pytest

from failsafe import (
    detection_live_demo,
    diagnosis_live_demo,
    mcp_server,
    razorpay_client,
    real_mcp_demo,
    recovery_pipeline,
    route_demo,
)
from failsafe.decline_codes import get_decline_code
from failsafe.recovery_pipeline import LEFT_ALONE_BY_DETECTION


def policy_following_proposer(record, description, source, **_kwargs):
    """Fake LLM: proposes exactly the policy action for the code it is given."""
    return {
        "action": get_decline_code(record["decline_code"]).allowed_action.value,
        "reasoning": "[test] follows the policy table",
    }


def _sub(sub_id, code, raw, amount=29900, days=3):
    return {
        "subscription_id": sub_id,
        "amount_paise": amount,
        "currency": "INR",
        "customer_id": f"cust_{sub_id}",
        "merchant_id": "merch_test",
        "decline_code": code,
        "raw_decline_message": raw,
        "plan": "Test Plan",
        "halted_days_ago": days,
        "previous_retry_count": 0,
        "simulated_customer_response": True,
    }


# ---------------------------------------------------------------- detection


@pytest.fixture
def detection_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(detection_live_demo, "AUDIT_PATH", tmp_path / "detection_audit.jsonl")
    monkeypatch.setattr(detection_live_demo, "RESULTS_PATH", tmp_path / "DETECTION_DEMO_RESULTS.md")
    return tmp_path


def _run_detection(n, wrong_ids=()):
    """Fake detector: right for every record except those in `wrong_ids`."""
    pool = json.loads(detection_live_demo.DATA_PATH.read_text(encoding="utf-8"))
    by_sig = {}
    for r in pool:
        key = (
            r["previous_retry_count"],
            r["days_since_last_successful_charge"],
            r["most_recent_gateway_response"],
            r["subscription_status"],
        )
        by_sig.setdefault(key, []).append(r)

    def fake_detect(
        previous_retry_count,
        days_since_last_successful_charge,
        most_recent_gateway_response,
        subscription_status,
    ):
        rec = by_sig[
            (
                previous_retry_count,
                days_since_last_successful_charge,
                most_recent_gateway_response,
                subscription_status,
            )
        ][0]
        truth = rec["ground_truth_needs_attention"]
        if rec["subscription_id"] in wrong_ids:
            truth = not truth
        return {
            "classification": "needs_recovery_attention" if truth else "leave_alone",
            "reasoning": "[test] fake detector",
        }

    raw_to_code = {
        r["raw_decline_message"]: r["decline_code"] for r in pool if r.get("raw_decline_message")
    }

    def fake_diagnose(raw, amount_paise=None):
        return {"decline_code": raw_to_code.get(raw), "reasoning": "[test] lookup"}

    with (
        patch.object(recovery_pipeline, "detect_at_risk", side_effect=fake_detect),
        patch.object(recovery_pipeline, "diagnose_decline_code", side_effect=fake_diagnose),
        recovery_pipeline.use_proposer(policy_following_proposer),
    ):
        return asyncio.run(detection_live_demo.run(n)), pool


def _unique_signature_records(pool):
    """Records whose four detection signals are unique in the pool (so the
    fake detector can tell them apart)."""
    from collections import Counter

    def sig(r):
        return (
            r["previous_retry_count"],
            r["days_since_last_successful_charge"],
            r["most_recent_gateway_response"],
            r["subscription_status"],
        )

    counts = Counter(sig(r) for r in pool)
    return [r for r in pool if counts[sig(r)] == 1]


def test_detection_demo_counts_false_negative_value_and_false_positive_waste(
    detection_paths, capsys
):
    pool = json.loads(detection_live_demo.DATA_PATH.read_text(encoding="utf-8"))
    unique = _unique_signature_records(pool)
    at_risk = next(r for r in unique if r["ground_truth_needs_attention"])
    healthy = next(r for r in unique if not r["ground_truth_needs_attention"])

    results, pool = _run_detection(
        None, wrong_ids={at_risk["subscription_id"], healthy["subscription_id"]}
    )

    assert len(results) == len(pool)
    by_id = {r["subscription_id"]: r for r in results}
    # wrongly cleared at-risk record: never diagnosed, gated or acted on
    assert by_id[at_risk["subscription_id"]]["final_action"] == LEFT_ALONE_BY_DETECTION
    # wrongly flagged healthy record: really went into the pipeline
    assert by_id[healthy["subscription_id"]]["final_action"] != LEFT_ALONE_BY_DETECTION

    text = (detection_paths / "DETECTION_DEMO_RESULTS.md").read_text(encoding="utf-8")
    n = len(pool)
    assert f"{n - 2}/{n}" in text
    assert "False positives (healthy, wrongly flagged 'needs_recovery_attention'): 1**" in text
    assert "of which **1** genuinely" in text
    assert f"Rs {at_risk['amount_paise'] / 100:,.2f}" in text
    assert "False negatives (at-risk, wrongly cleared 'leave_alone'): 1**" in text

    events = [
        json.loads(line)
        for line in (detection_paths / "detection_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events[0]["event_type"] == "detection_demo_started"
    assert events[-1]["event_type"] == "detection_demo_finished"
    assert "Detection accuracy" in capsys.readouterr().out


def test_detection_demo_perfect_detector_and_n_limit(detection_paths):
    results, _ = _run_detection(5)
    assert len(results) == 5
    assert all(r["detection_matched_ground_truth"] for r in results)
    text = (detection_paths / "DETECTION_DEMO_RESULTS.md").read_text(encoding="utf-8")
    assert "5/5 (100.0%)" in text


def test_detection_demo_resets_its_own_audit_log(detection_paths):
    (detection_paths / "detection_audit.jsonl").write_text(
        '{"event_type": "stale"}\n', encoding="utf-8"
    )
    _run_detection(2)
    events = (detection_paths / "detection_audit.jsonl").read_text(encoding="utf-8")
    assert "stale" not in events


def test_detection_write_results_empty_run_does_not_crash(detection_paths, capsys):
    detection_live_demo.write_results([])
    assert "No records processed" in (detection_paths / "DETECTION_DEMO_RESULTS.md").read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------- diagnosis


@pytest.fixture
def diagnosis_env(tmp_path, monkeypatch):
    subs = [
        _sub("sub_t1", "insufficient_funds", "Insufficient balance in account"),
        _sub("sub_t2", "card_expired", "Card has expired"),
        _sub("sub_t3", "payment_timed_out", "Request timed out at bank"),
        _sub("sub_t4", "insufficient_funds", "Not enough funds"),
        _sub("sub_t5", "gateway_technical_error", "Gateway hiccup"),
    ]
    data = tmp_path / "subs.json"
    data.write_text(json.dumps(subs), encoding="utf-8")
    monkeypatch.setattr(diagnosis_live_demo, "DATA_PATH", data)
    monkeypatch.setattr(diagnosis_live_demo, "AUDIT_PATH", tmp_path / "diag_audit.jsonl")
    monkeypatch.setattr(diagnosis_live_demo, "RESULTS_PATH", tmp_path / "DIAGNOSIS_DEMO_RESULTS.md")
    return tmp_path, subs


def test_diagnosis_demo_counts_only_misdiagnoses_that_change_the_action(diagnosis_env, capsys):
    tmp, subs = diagnosis_env
    diagnosed = {
        "Insufficient balance in account": "insufficient_funds",  # right
        "Card has expired": "card_expired",  # right
        # wrong, but payment_timed_out and gateway_technical_error share
        # immediate_retry, so the final action does not change
        "Request timed out at bank": "gateway_technical_error",
        # wrong AND consequential: delayed_retry -> payment_link_nudge
        "Not enough funds": "card_expired",
        "Gateway hiccup": None,  # diagnosis failure
    }

    def fake_diagnose(raw, amount_paise=None):
        return {"decline_code": diagnosed[raw], "reasoning": "[test]"}

    with (
        patch.object(recovery_pipeline, "diagnose_decline_code", side_effect=fake_diagnose),
        recovery_pipeline.use_proposer(policy_following_proposer),
    ):
        results = asyncio.run(diagnosis_live_demo.run(len(subs)))

    by_id = {r["subscription_id"]: r for r in results}
    assert by_id["sub_t3"]["final_action"] == "immediate_retry"
    assert by_id["sub_t4"]["final_action"] == "payment_link_nudge"
    assert by_id["sub_t5"]["diagnosed_decline_code"] is None

    text = (tmp / "DIAGNOSIS_DEMO_RESULTS.md").read_text(encoding="utf-8")
    assert "2/5 (40.0%)" in text
    assert (
        "Misdiagnoses: **3/5**, of which diagnosis failures (no usable tool call / unrecognized code returned): **1**"
        in text
    )
    # sub_t4 (delayed -> nudge) and sub_t5 (failure -> flagged instead of immediate_retry)
    assert "truth's own policy would have given: **2/5**" in text
    assert "| sub_t1 | Insufficient balance in account | insufficient_funds |" in text
    assert "Final-action changed by misdiagnosis: 2/5" in capsys.readouterr().out


def test_diagnosis_write_results_empty_run_does_not_crash(diagnosis_env):
    tmp, _ = diagnosis_env
    diagnosis_live_demo.write_results([])
    assert "No records processed" in (tmp / "DIAGNOSIS_DEMO_RESULTS.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- real MCP demo


def test_real_mcp_pick_sample_covers_both_money_tools():
    subs = json.loads(real_mcp_demo.DATA_PATH.read_text(encoding="utf-8"))
    sample = real_mcp_demo._pick_sample(subs, 5)
    tools = {
        real_mcp_demo.ACTION_TO_TOOL[get_decline_code(s["decline_code"]).allowed_action]
        for s in sample
    }
    assert tools == {"create_retry_order", "create_payment_link"}
    assert len({s["decline_code"] for s in sample}) == len(sample)
    assert len(real_mcp_demo._pick_sample(subs, 2)) == 2


def test_real_mcp_pick_sample_skips_missing_codes():
    subs = [_sub("sub_only", "card_expired", "x")]
    assert [s["subscription_id"] for s in real_mcp_demo._pick_sample(subs, 5)] == ["sub_only"]


def test_real_mcp_demo_refuses_without_real_keys(monkeypatch):
    monkeypatch.setattr(razorpay_client, "keys_configured", lambda: False)
    with pytest.raises(SystemExit, match="rzp_test_"):
        asyncio.run(real_mcp_demo.run(1))


def test_real_mcp_demo_refuses_without_data(monkeypatch, tmp_path):
    monkeypatch.setattr(razorpay_client, "keys_configured", lambda: True)
    monkeypatch.setattr(real_mcp_demo, "DATA_PATH", tmp_path / "missing.json")
    with pytest.raises(SystemExit, match="No data"):
        asyncio.run(real_mcp_demo.run(1))


def test_real_mcp_demo_writes_log_and_report(monkeypatch, tmp_path, capsys):
    # Pretend real keys exist so run() proceeds, but force the in-process
    # MCP server into SIMULATE so no call ever reaches Razorpay.
    monkeypatch.setattr(razorpay_client, "keys_configured", lambda: True)
    monkeypatch.setattr(real_mcp_demo, "LOG_PATH", tmp_path / "logs" / "real.jsonl")
    monkeypatch.setattr(real_mcp_demo, "RESULTS_PATH", tmp_path / "REAL_MCP_RESULTS.md")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "real.jsonl").write_text('{"old": true}\n', encoding="utf-8")

    with mcp_server.simulate_mode(True):
        asyncio.run(real_mcp_demo.run(5))

    entries = [
        json.loads(line)
        for line in (tmp_path / "logs" / "real.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(entries) == 5
    assert all("old" not in e for e in entries)  # overwritten, not appended
    assert {e["tool"] for e in entries} == {"create_retry_order", "create_payment_link"}
    ids = [json.loads(e["tool_result"])["id"] for e in entries]
    assert all("_sim_" in i for i in ids), ids

    report = (tmp_path / "REAL_MCP_RESULTS.md").read_text(encoding="utf-8")
    for e, i in zip(entries, ids, strict=True):
        assert f"| {e['subscription_id']} | {e['decline_code']} |" in report
        assert f"`{i}`" in report
    assert "real Razorpay id:" in capsys.readouterr().out


# ---------------------------------------------------------------- route demo


def test_route_demo_blocks_over_cap_and_pays_partner_share(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(route_demo, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(route_demo, "RESULTS_PATH", tmp_path / "ROUTE_RESULTS.md")

    with mcp_server.simulate_mode(True):
        asyncio.run(route_demo.run())

    events = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    blocked = [e for e in events if e["event_type"] == "route_transfer_blocked"]
    executed = [e for e in events if e["event_type"] == "route_transfer"]
    assert [e["subscription_id"] for e in blocked] == ["sub_route_demo_004"]
    assert "sub_route_demo_004" not in {e["subscription_id"] for e in executed}
    assert len(executed) == 4
    for e in executed:
        assert e["partner_share_paise"] == int(e["amount_paise"] * route_demo.PARTNER_SHARE_PCT)
        assert e["partner_linked_account_id"] == mcp_server.PARTNER_LINKED_ACCOUNT_ID
        assert e["result"]

    total_share = sum(e["partner_share_paise"] for e in executed)
    report = (tmp_path / "ROUTE_RESULTS.md").read_text(encoding="utf-8")
    assert "Route transfers executed: **4**" in report
    assert "Blocked by the spending-cap gate: **1**" in report
    assert f"Rs {total_share / 100:,.2f}" in report
    assert "| sub_route_demo_004 | partner_affiliate_03 |" in report and "BLOCKED (cap)" in report
    assert "BLOCKED (exceeds spending cap)" in capsys.readouterr().out


def test_route_write_results_labels_simulated_linked_account(monkeypatch, tmp_path):
    monkeypatch.setattr(route_demo, "RESULTS_PATH", tmp_path / "ROUTE_RESULTS.md")
    monkeypatch.setattr(route_demo, "PARTNER_LINKED_ACCOUNT_ID", "acc_sim_partner")
    route_demo.write_results([])
    text = (tmp_path / "ROUTE_RESULTS.md").read_text(encoding="utf-8")
    assert "simulated - no real Linked Account" in text
    assert "Recoveries processed: **0**" in text
    monkeypatch.setattr(route_demo, "PARTNER_LINKED_ACCOUNT_ID", "acc_RealOne123")
    route_demo.write_results([])
    assert "a real onboarded account" in (tmp_path / "ROUTE_RESULTS.md").read_text(encoding="utf-8")
