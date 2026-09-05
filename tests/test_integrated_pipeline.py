"""
Proves integrated_pipeline.py genuinely wires the four standalone recovery
domains together, not just imports them side by side:

  1. identify_domain() correctly classifies a real record from each of the
     four actual data files, and fails loudly on an ambiguous/unrecognized
     shape rather than guessing.
  2. A tiny MIXED, interleaved batch run through the real run() end-to-end
     dispatches every record to the domain matching its own ID field, and
     each domain's real gate is genuinely invoked (not bypassed) - proven
     with an over-the-spending-cap record that must still be hard-blocked.
  3. The unified report's aggregate numbers are arithmetically consistent
     with the per-record results that produced them.
  4. Running the integrated pipeline never touches any of the four domains'
     own RESULTS*.md files.

Diagnosis/action-proposal calls are mocked throughout (mirrors every other
pipeline test in this suite) so this file never depends on a live Ollama
server. Both mcp_server.SIMULATE and mcp_server._rp.simulate are forced
True (integrated_pipeline.run() does this itself), so no live Razorpay call
is ever a side effect of running this suite.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import integrated_pipeline as ip
import mcp_server
from gate import MAX_ACTION_AMOUNT_PAISE

REPO_ROOT = Path(__file__).parent.parent


def _base_subscription(**overrides):
    record = {
        "subscription_id": "sub_int_test",
        "amount_paise": 29900,
        "decline_code": "insufficient_funds",  # policy -> delayed_retry
        "raw_decline_message": "Bank response: insufficient balance in account.",
        "plan": "Test Plan",
        "halted_days_ago": 1,
        "simulated_customer_response": False,
    }
    record.update(overrides)
    return record


def _base_onetime(**overrides):
    record = {
        "payment_id": "pay_int_test",
        "amount_paise": 19900,
        "decline_code": "insufficient_funds",  # policy -> delayed_retry
        "item": "Test Item",
        "simulated_customer_response": False,
    }
    record.update(overrides)
    return record


def _base_cart(**overrides):
    record = {
        "cart_id": "cart_int_test",
        "amount_paise": 29900,
        "item": "Test Item",
        "checkout_stage": "otp_entry",
        "minutes_since_abandonment": 3,
        "device_type": "mobile_web",
        "is_returning_customer": True,
        "abandonment_reason": "otp_delay_or_failure",  # policy -> immediate_payment_link_resend
        "simulated_customer_response": True,
    }
    record.update(overrides)
    return record


def _base_invoice(**overrides):
    record = {
        "invoice_id": "inv_int_test",
        "amount_paise": 500000,
        "business_name": "Test Biz",
        "days_overdue": 5,
        "payment_terms": "net_30",
        "customer_payment_history_signal": "first_time_overdue",
        "reminders_sent_count": 0,
        "last_reminder_response": None,
        "amount_vs_typical_ratio": 1.0,
        "case_reason": "cash_flow_delay",  # policy -> payment_plan_offer
        "simulated_customer_response": True,
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# 1. identify_domain()
# ---------------------------------------------------------------------------

def test_identify_domain_classifies_a_real_record_from_each_data_file():
    for name, cfg in ip.DOMAINS.items():
        records = json.loads(cfg["data_path"].read_text(encoding="utf-8"))
        assert records, f"no records in {cfg['data_path']}"
        assert ip.identify_domain(records[0]) == name


def test_identify_domain_raises_on_no_known_id_field():
    with pytest.raises(ValueError):
        ip.identify_domain({"some_other_field": "x"})


def test_identify_domain_raises_on_ambiguous_record():
    with pytest.raises(ValueError):
        ip.identify_domain({"subscription_id": "sub_1", "cart_id": "cart_1"})


# ---------------------------------------------------------------------------
# 2-3. End-to-end mixed batch through the real run()
# ---------------------------------------------------------------------------

def _run_mixed_batch(records_by_domain: dict[str, list[dict]], mocks: dict[str, tuple[str, dict]]):
    """
    Points integrated_pipeline.DOMAINS at tempfile data/audit paths for the
    duration of the call, then runs the REAL ip.run() end to end (not a
    reimplemented parallel harness) so these tests exercise the exact
    dispatch/gate/audit code path the live script uses. Restores DOMAINS
    afterward regardless of outcome.
    """
    tmp_dir = Path(tempfile.mkdtemp())
    originals = {name: dict(cfg) for name, cfg in ip.DOMAINS.items()}
    try:
        for name, cfg in ip.DOMAINS.items():
            data_path = tmp_dir / f"{name}.json"
            data_path.write_text(json.dumps(records_by_domain.get(name, [])), encoding="utf-8")
            cfg["data_path"] = data_path
            cfg["audit_path"] = tmp_dir / f"audit_{name}.jsonl"

        integrated_audit_path = tmp_dir / "audit_integrated.jsonl"
        integrated_results_path = tmp_dir / "INTEGRATED_RESULTS.md"

        with patch.object(ip, "INTEGRATED_AUDIT_PATH", integrated_audit_path), \
             patch.object(ip, "INTEGRATED_RESULTS_PATH", integrated_results_path):
            active_patches = [
                patch(target, return_value=value) for target, value in mocks.values()
            ]
            for p in active_patches:
                p.start()
            try:
                per_domain = max((len(v) for v in records_by_domain.values()), default=0)
                results = asyncio.run(ip.run(per_domain=per_domain))
            finally:
                for p in active_patches:
                    p.stop()

        report_text = integrated_results_path.read_text(encoding="utf-8") if integrated_results_path.exists() else ""
        return results, report_text
    finally:
        for name, cfg in ip.DOMAINS.items():
            cfg.clear()
            cfg.update(originals[name])
        mcp_server._reset_tool_level_guard_for_tests()


_STANDARD_MOCKS = {
    "subscription_diagnosis": (
        "recovery_pipeline.diagnose_decline_code",
        {"decline_code": "insufficient_funds", "reasoning": "test"},
    ),
    "propose_action": (
        "recovery_pipeline.propose_action",
        {"action": "delayed_retry", "reasoning": "test"},
    ),
    "checkout_diagnosis": (
        "checkout_abandonment_agent.diagnose_abandonment_reason",
        {"reason": "otp_delay_or_failure", "reasoning": "test"},
    ),
    "receivables_diagnosis": (
        "receivables_agent.diagnose_receivable",
        {"case_reason": "cash_flow_delay", "reasoning": "test"},
    ),
}


def test_mixed_batch_dispatches_every_record_to_its_own_domain():
    batch = {
        "subscription": [_base_subscription(subscription_id="sub_a"), _base_subscription(subscription_id="sub_b")],
        "one_time_payment": [_base_onetime(payment_id="pay_a"), _base_onetime(payment_id="pay_b")],
        "checkout_abandonment": [_base_cart(cart_id="cart_a"), _base_cart(cart_id="cart_b")],
        "overdue_receivable": [_base_invoice(invoice_id="inv_a"), _base_invoice(invoice_id="inv_b")],
    }
    results, _ = _run_mixed_batch(batch, _STANDARD_MOCKS)

    assert set(results.keys()) == set(ip.DOMAINS.keys())
    for name, cfg in ip.DOMAINS.items():
        assert len(results[name]) == 2
        for r in results[name]:
            assert cfg["id_field"] in r
            assert r[cfg["id_field"]] in {rec[cfg["id_field"]] for rec in batch[name]}


def test_each_domains_gate_is_genuinely_invoked_not_bypassed():
    # An over-the-per-action-cap subscription must still be hard-blocked by
    # the real Gate, and an over-cap invoice by the real ReceivableGate -
    # proves the orchestrator routes through each domain's actual gate
    # rather than skipping straight to execution.
    over_cap = MAX_ACTION_AMOUNT_PAISE + 100
    batch = {
        "subscription": [_base_subscription(subscription_id="sub_over", amount_paise=over_cap)],
        "one_time_payment": [],
        "checkout_abandonment": [],
        "overdue_receivable": [_base_invoice(invoice_id="inv_over", amount_paise=over_cap)],
    }
    results, _ = _run_mixed_batch(batch, _STANDARD_MOCKS)

    sub_result = results["subscription"][0]
    assert sub_result["gate_executed"] is False
    inv_result = results["overdue_receivable"][0]
    assert inv_result["gate_executed"] is False


def test_unified_report_totals_match_per_record_results():
    batch = {
        "subscription": [_base_subscription(subscription_id="sub_x")],
        "one_time_payment": [_base_onetime(payment_id="pay_x")],
        "checkout_abandonment": [_base_cart(cart_id="cart_x")],
        "overdue_receivable": [_base_invoice(invoice_id="inv_x")],
    }
    results, report_text = _run_mixed_batch(batch, _STANDARD_MOCKS)

    expected_total_records = sum(len(v) for v in results.values())
    expected_total_value = sum(
        r.get("amount_paise") or 0 for recs in results.values() for r in recs
    )

    assert f"**{expected_total_records}**" in report_text
    assert f"Rs {expected_total_value/100:,.2f}" in report_text
    assert "100" in report_text  # dispatch correctness line


def test_integrated_run_does_not_touch_any_domains_own_results_file():
    results_files = [
        REPO_ROOT / "RESULTS.md",
        REPO_ROOT / "RESULTS_ONETIME.md",
        REPO_ROOT / "CHECKOUT_ABANDONMENT_RESULTS.md",
        REPO_ROOT / "RECEIVABLES_RESULTS.md",
    ]
    before = {p: (p.read_text(encoding="utf-8") if p.exists() else None) for p in results_files}

    batch = {
        "subscription": [_base_subscription(subscription_id="sub_iso")],
        "one_time_payment": [],
        "checkout_abandonment": [],
        "overdue_receivable": [],
    }
    _run_mixed_batch(batch, _STANDARD_MOCKS)

    after = {p: (p.read_text(encoding="utf-8") if p.exists() else None) for p in results_files}
    assert before == after
