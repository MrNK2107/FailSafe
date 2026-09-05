"""
Unit tests for generate_detection_pool.py's MIXED-POOL dataset - the input
to the new revenue-at-risk DETECTION stage (detect.py). Mirrors
test_generate_data.py's style for the sibling generator.

Real risks this locks in:
  1. The pool actually contains BOTH genuinely healthy and genuinely
     at-risk records, not all one or the other - a "detection" step run
     against an all-healthy or all-at-risk pool couldn't prove anything.
  2. A healthy record never carries a decline_code/raw_decline_message -
     handing detect.py an answer key disguised as a signal would make
     "detection" a lookup, not a classification task.
  3. detect.py's four signal fields are ALWAYS present (never a
     precomputed is_at_risk/needs_attention boolean handed to it).
  4. The deliberately-ambiguous "early at-risk" / "healthy blip" slices
     genuinely overlap in retry-count/day-range/status, mirroring
     generate_data.py's shared-raw-text-pool ambiguity clusters.
  5. Determinism given a seed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from decline_codes import DECLINE_CODES
from generate_detection_pool import AT_RISK_CODE_WEIGHTS, generate


def test_pool_contains_both_healthy_and_at_risk_records():
    records = generate(n=60, seed=1)
    healthy = [r for r in records if not r["ground_truth_needs_attention"]]
    at_risk = [r for r in records if r["ground_truth_needs_attention"]]
    assert len(healthy) >= 10, "pool should contain a real number of genuinely healthy records"
    assert len(at_risk) >= 10, "pool should contain a real number of genuinely at-risk records"


def test_healthy_records_never_carry_a_decline_code_or_raw_decline_message():
    records = generate(n=60, seed=1)
    for r in records:
        if not r["ground_truth_needs_attention"]:
            assert r["decline_code"] is None, "a healthy record must not carry a ground-truth decline_code"
            assert r["raw_decline_message"] is None, "a healthy record must not carry a raw decline message"


def test_at_risk_records_carry_a_real_decline_code_and_matching_raw_message():
    records = generate(n=60, seed=1)
    for r in records:
        if r["ground_truth_needs_attention"]:
            assert r["decline_code"] in DECLINE_CODES
            assert r["raw_decline_message"] is not None
            assert r["most_recent_gateway_response"] == r["raw_decline_message"]


def test_every_record_carries_all_four_detection_signal_fields():
    # detect.py's ONLY inputs. No record should be missing any of them, and
    # none of them may be a precomputed "is this at risk" boolean.
    records = generate(n=40, seed=2)
    for r in records:
        assert "previous_retry_count" in r
        assert "days_since_last_successful_charge" in r
        assert "most_recent_gateway_response" in r
        assert "subscription_status" in r
        assert isinstance(r["previous_retry_count"], int)
        assert isinstance(r["days_since_last_successful_charge"], int)
        assert r["subscription_status"] in ("active", "pending", "halted")


def test_no_precomputed_is_at_risk_boolean_is_exposed_as_a_signal():
    # ground_truth_needs_attention is allowed to exist ON THE RECORD (same
    # role decline_code plays for diagnose.py - ground truth kept only for
    # scoring), but detect.py's own function signature never accepts it -
    # verified structurally here by confirming it's not one of the four
    # signal field names detect.detect_at_risk() takes.
    import inspect

    import detect
    params = set(inspect.signature(detect.detect_at_risk).parameters)
    assert "ground_truth_needs_attention" not in params
    assert params == {
        "previous_retry_count", "days_since_last_successful_charge",
        "most_recent_gateway_response", "subscription_status",
    }


def test_ambiguous_early_slice_overlaps_between_healthy_blip_and_early_at_risk():
    # The deliberate ambiguity: a "pending" status, 1 prior retry, and a
    # small day-count appears on BOTH sides of ground truth - a real
    # classification task, not a lookup on subscription_status alone.
    records = generate(n=150, seed=3)
    pending_one_retry = [
        r for r in records
        if r["subscription_status"] == "pending" and r["previous_retry_count"] == 1
    ]
    truths = {r["ground_truth_needs_attention"] for r in pending_one_retry}
    assert truths == {True, False}, (
        "status='pending' + previous_retry_count=1 should appear on BOTH sides of "
        "ground truth for detection to be a genuine classification task"
    )


def test_every_at_risk_weighted_code_is_a_real_decline_code():
    for code in AT_RISK_CODE_WEIGHTS:
        assert code in DECLINE_CODES


def test_generation_is_deterministic_given_a_seed():
    # subscription_id/customer_id use uuid.uuid4() (unseeded, same as
    # generate_data.py's own records) and always differ - everything
    # derived from the seeded RNG must not.
    a = generate(n=30, seed=99)
    b = generate(n=30, seed=99)
    assert [r["ground_truth_needs_attention"] for r in a] == [r["ground_truth_needs_attention"] for r in b]
    assert [r["decline_code"] for r in a] == [r["decline_code"] for r in b]
    assert [r["previous_retry_count"] for r in a] == [r["previous_retry_count"] for r in b]
    assert [r["subscription_status"] for r in a] == [r["subscription_status"] for r in b]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
