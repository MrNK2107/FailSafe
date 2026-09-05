"""
Unit tests for generate_data_onetime.py - the stretch-goal generator for
failed one-time payments. Checks the same invariants test_decline_codes.py
checks for the main dataset: every generated record must reference a real
decline code and have a plausible amount, since a schema mismatch here
would silently break agent_onetime.py's gate calls.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from decline_codes import DECLINE_CODES
from generate_data_onetime import CODE_WEIGHTS, generate


def test_every_weighted_code_is_a_real_decline_code():
    for code in CODE_WEIGHTS:
        assert code in DECLINE_CODES, f"{code} in CODE_WEIGHTS is not a real decline code"


def test_generated_records_have_required_fields_and_valid_codes():
    records = generate(n=20)
    assert len(records) == 20
    seen_ids = set()
    for r in records:
        assert r["decline_code"] in DECLINE_CODES
        assert r["amount_paise"] > 0
        assert r["payment_id"] not in seen_ids, "duplicate payment_id generated"
        seen_ids.add(r["payment_id"])
        assert isinstance(r["simulated_customer_response"], bool)
        # Unlike a subscription record, a one-time payment must NOT carry
        # fields that imply Razorpay already retried it - that's the whole
        # domain distinction (BUILD_LOG.md §13).
        assert "previous_retry_count" not in r
        assert "halted_days_ago" not in r


def test_generation_is_deterministic_given_a_seed():
    a = generate(n=15, seed=99)
    b = generate(n=15, seed=99)
    a_codes = [r["decline_code"] for r in a]
    b_codes = [r["decline_code"] for r in b]
    assert a_codes == b_codes


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
