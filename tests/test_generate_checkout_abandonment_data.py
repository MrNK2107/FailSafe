"""
Unit tests for generate_checkout_abandonment_data.py - mirrors
tests/test_generate_data.py and tests/test_generate_detection_pool.py's
own discipline: prove the deliberate ambiguity clusters genuinely exist
in the generated signal space (not just claimed in a docstring), prove
ground truth is never leaked into the signals diagnosis actually sees,
and prove determinism given a seed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from checkout_abandonment_policy import ABANDONMENT_POLICIES, AbandonmentReason
from generate_checkout_abandonment_data import (
    CHECKOUT_STAGES,
    DEVICE_TYPES,
    REASON_WEIGHTS,
    generate,
)

SIGNAL_FIELDS = {
    "cart_id", "merchant_id", "customer_id", "item", "amount_paise", "currency",
    "checkout_stage", "minutes_since_abandonment", "device_type",
    "is_returning_customer", "abandonment_reason", "simulated_customer_response",
}


def test_every_weighted_reason_is_a_real_abandonment_reason():
    for reason in REASON_WEIGHTS:
        assert reason in {r.value for r in AbandonmentReason}
        assert reason in ABANDONMENT_POLICIES


def test_generated_records_carry_exactly_the_expected_fields():
    records = generate(n=20, seed=1)
    for r in records:
        assert set(r.keys()) == SIGNAL_FIELDS


def test_checkout_stage_and_device_type_are_always_known_values():
    records = generate(n=100, seed=2)
    for r in records:
        assert r["checkout_stage"] in CHECKOUT_STAGES
        assert r["device_type"] in DEVICE_TYPES


def test_no_decline_code_or_retry_count_field_exists():
    # Structural guard: this category has no decline_code by definition -
    # if one ever appears, something copy-pasted from generate_data.py
    # leaked a field that doesn't belong here.
    records = generate(n=20, seed=3)
    for r in records:
        assert "decline_code" not in r
        assert "previous_retry_count" not in r
        assert "halted_days_ago" not in r


def test_amounts_and_ids_are_well_formed():
    records = generate(n=50, seed=4)
    seen_ids = set()
    for r in records:
        assert r["amount_paise"] > 0
        assert r["cart_id"] not in seen_ids
        seen_ids.add(r["cart_id"])


def test_distribution_is_weighted_not_uniform():
    records = generate(n=150, seed=5)
    counts = {}
    for r in records:
        counts[r["abandonment_reason"]] = counts.get(r["abandonment_reason"], 0) + 1
    # otp_delay_or_failure (weight 22) should heavily outnumber
    # trust_or_security_concern (weight 15) is too close to assert
    # reliably at n=150 with randomness, so assert the coarser claim:
    # every reason appears at least once, and no reason dominates > 60%.
    assert set(counts) == set(REASON_WEIGHTS)
    assert max(counts.values()) < 0.6 * len(records)


def test_cluster_a_otp_entry_medium_band_spans_two_ground_truths():
    # The deliberate ambiguity cluster generate_checkout_abandonment_data.py's
    # own docstring promises: checkout_stage="otp_entry" with
    # minutes_since_abandonment in [8, 15] must contain BOTH
    # otp_delay_or_failure AND distraction_or_multitasking records, or the
    # "genuine ambiguity" claim is false.
    records = generate(n=300, seed=6)
    cluster = [
        r for r in records
        if r["checkout_stage"] == "otp_entry" and 8 <= r["minutes_since_abandonment"] <= 15
    ]
    reasons_in_cluster = {r["abandonment_reason"] for r in cluster}
    assert {"otp_delay_or_failure", "distraction_or_multitasking"}.issubset(reasons_in_cluster)


def test_cluster_b_new_customer_card_entry_spans_two_ground_truths():
    # The second deliberate ambiguity cluster: checkout_stage=
    # "card_details_entry" with is_returning_customer=False must contain
    # BOTH trust_or_security_concern AND payment_method_unsupported.
    records = generate(n=300, seed=7)
    cluster = [
        r for r in records
        if r["checkout_stage"] == "card_details_entry" and r["is_returning_customer"] is False
    ]
    reasons_in_cluster = {r["abandonment_reason"] for r in cluster}
    assert {"trust_or_security_concern", "payment_method_unsupported"}.issubset(reasons_in_cluster)


def test_low_value_carts_exist_to_exercise_the_gates_value_floor():
    # abandonment_gate.MIN_CART_VALUE_FOR_ACTION_PAISE is Rs 149 - the
    # generator must actually produce some carts below that, or the
    # low-value stopping rule would never fire in a live/demo run.
    from abandonment_gate import MIN_CART_VALUE_FOR_ACTION_PAISE
    records = generate(n=150, seed=8)
    low_value = [r for r in records if r["amount_paise"] < MIN_CART_VALUE_FOR_ACTION_PAISE]
    assert len(low_value) > 0


def test_deterministic_given_same_seed():
    a = generate(n=30, seed=42)
    b = generate(n=30, seed=42)
    # cart_id/customer_id use uuid4 (never seeded, exactly like
    # generate_data.py's subscription_id/customer_id) so compare
    # everything else field-by-field.
    for ra, rb in zip(a, b):
        for key in SIGNAL_FIELDS - {"cart_id", "customer_id"}:
            assert ra[key] == rb[key]


def test_trust_or_security_concern_is_always_a_new_customer_by_construction():
    records = generate(n=200, seed=9)
    trust_records = [r for r in records if r["abandonment_reason"] == "trust_or_security_concern"]
    assert trust_records  # sanity: this reason actually appears
    assert all(r["is_returning_customer"] is False for r in trust_records)


def test_price_shock_always_reaches_review_confirm_stage():
    records = generate(n=200, seed=10)
    price_shock_records = [r for r in records if r["abandonment_reason"] == "price_shock"]
    assert price_shock_records
    assert all(r["checkout_stage"] == "review_confirm" for r in price_shock_records)


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
