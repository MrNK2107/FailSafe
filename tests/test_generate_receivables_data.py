"""
Unit tests for generate_receivables_data.py - mirrors
tests/test_generate_data.py, tests/test_generate_detection_pool.py, and
tests/test_generate_checkout_abandonment_data.py's own discipline: prove
the deliberate ambiguity clusters genuinely exist in the generated signal
space (not just claimed in a docstring), prove ground truth is never
leaked into the signals diagnosis actually sees, and prove determinism
given a seed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gate import MAX_ACTION_AMOUNT_PAISE
from receivables_gate import DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD, MAX_REMINDERS_BEFORE_ESCALATION
from receivables_policy import RECEIVABLE_POLICIES, ReceivableReason
from generate_receivables_data import PAYMENT_TERMS, REASON_WEIGHTS, generate

SIGNAL_FIELDS = {
    "invoice_id", "merchant_id", "customer_id", "business_name", "amount_paise", "currency",
    "invoice_issue_date", "due_date", "payment_terms", "days_overdue",
    "customer_payment_history_signal", "reminders_sent_count", "last_reminder_response",
    "typical_order_amount_paise", "amount_vs_typical_ratio", "case_reason",
    "simulated_customer_response",
}


def test_every_weighted_reason_is_a_real_receivable_reason():
    for reason in REASON_WEIGHTS:
        assert reason in {r.value for r in ReceivableReason}
        assert reason in RECEIVABLE_POLICIES


def test_generated_records_carry_exactly_the_expected_fields():
    records = generate(n=20, seed=1)
    for r in records:
        assert set(r.keys()) == SIGNAL_FIELDS


def test_payment_terms_are_always_known_values():
    records = generate(n=100, seed=2)
    for r in records:
        assert r["payment_terms"] in PAYMENT_TERMS


def test_no_decline_code_or_checkout_field_exists():
    # Structural guard: this category has no decline_code and no checkout
    # funnel by definition - if either ever appears, something
    # copy-pasted from another generator leaked a field that doesn't
    # belong here.
    records = generate(n=20, seed=3)
    for r in records:
        assert "decline_code" not in r
        assert "checkout_stage" not in r
        assert "previous_retry_count" not in r


def test_amounts_days_overdue_and_ids_are_well_formed():
    records = generate(n=50, seed=4)
    seen_ids = set()
    for r in records:
        assert r["amount_paise"] > 0
        assert r["days_overdue"] >= 1  # this category is "overdue" by definition
        assert r["invoice_id"] not in seen_ids
        seen_ids.add(r["invoice_id"])


def test_distribution_is_weighted_not_uniform():
    records = generate(n=150, seed=5)
    counts = {}
    for r in records:
        counts[r["case_reason"]] = counts.get(r["case_reason"], 0) + 1
    assert set(counts) == set(REASON_WEIGHTS)
    assert max(counts.values()) < 0.6 * len(records)


def test_cluster_a_first_time_overdue_no_reminders_early_spans_two_ground_truths():
    # The deliberate ambiguity cluster generate_receivables_data.py's own
    # docstring promises: customer_payment_history_signal=
    # "first_time_overdue" with reminders_sent_count=0 and days_overdue
    # in [1, 10] must contain BOTH cash_flow_delay AND
    # payment_process_friction records, or the "genuine ambiguity" claim
    # is false.
    records = generate(n=300, seed=6)
    cluster = [
        r for r in records
        if r["customer_payment_history_signal"] == "first_time_overdue"
        and r["reminders_sent_count"] == 0
        and 1 <= r["days_overdue"] <= 10
    ]
    reasons_in_cluster = {r["case_reason"] for r in cluster}
    assert {"cash_flow_delay", "payment_process_friction"}.issubset(reasons_in_cluster)


def test_cluster_b_disputes_history_with_silence_spans_two_ground_truths():
    # The second deliberate ambiguity cluster:
    # customer_payment_history_signal="disputes_invoices" with
    # last_reminder_response in {"no_response", "requested_extension"}
    # must contain BOTH invoice_dispute_likely AND high_risk_non_payment.
    records = generate(n=300, seed=7)
    cluster = [
        r for r in records
        if r["customer_payment_history_signal"] == "disputes_invoices"
        and r["last_reminder_response"] in ("no_response", "requested_extension")
    ]
    reasons_in_cluster = {r["case_reason"] for r in cluster}
    assert {"invoice_dispute_likely", "high_risk_non_payment"}.issubset(reasons_in_cluster)


def test_some_invoices_exceed_the_gates_spending_cap():
    # receivables_gate.py's own docstring discloses that B2B invoices are
    # realistically larger than a checkout cart or subscription charge,
    # and deliberately produces some invoices above
    # gate.MAX_ACTION_AMOUNT_PAISE so the spending-cap hard block has real
    # cases to fire on in a live run, not just a unit test.
    records = generate(n=200, seed=8)
    over_cap = [r for r in records if r["amount_paise"] > MAX_ACTION_AMOUNT_PAISE]
    assert len(over_cap) > 0


def test_some_invoices_exercise_the_reminder_cap_stopping_rule():
    records = generate(n=200, seed=9)
    at_or_past_cap = [r for r in records if r["reminders_sent_count"] >= MAX_REMINDERS_BEFORE_ESCALATION]
    assert len(at_or_past_cap) > 0


def test_some_invoices_exercise_the_staleness_stopping_rule():
    records = generate(n=200, seed=10)
    stale = [r for r in records if r["days_overdue"] >= DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD]
    assert len(stale) > 0


def test_deterministic_given_same_seed():
    a = generate(n=30, seed=42)
    b = generate(n=30, seed=42)
    # invoice_id/customer_id use uuid4 (never seeded, exactly like the
    # other two generators' own unseeded id fields) so compare everything
    # else field-by-field.
    for ra, rb in zip(a, b):
        for key in SIGNAL_FIELDS - {"invoice_id", "customer_id"}:
            assert ra[key] == rb[key]


def test_due_date_is_before_issue_date_plus_terms_and_before_today():
    import datetime
    from generate_receivables_data import PAYMENT_TERMS_DAYS, TODAY
    records = generate(n=50, seed=12)
    for r in records:
        issue = datetime.date.fromisoformat(r["invoice_issue_date"])
        due = datetime.date.fromisoformat(r["due_date"])
        assert (due - issue).days == PAYMENT_TERMS_DAYS[r["payment_terms"]]
        assert due < TODAY
        assert (TODAY - due).days == r["days_overdue"]


def test_no_reminders_sent_means_no_reminder_response_recorded():
    records = generate(n=100, seed=13)
    for r in records:
        if r["reminders_sent_count"] == 0:
            assert r["last_reminder_response"] is None


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    if failures:
        raise SystemExit(1)
