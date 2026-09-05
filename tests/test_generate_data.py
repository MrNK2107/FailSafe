"""
Unit tests for generate_data.py's raw-signal diagnosis input, added
alongside diagnose.py (BUILD_LOG.md §14). Mirrors
test_generate_data_onetime.py's style for the sibling generator.

Two real risks this locks in:
  1. RAW_SIGNAL_TEMPLATES must cover every real decline code, and no
     template may leak the decline_code string itself - a diagnosis task
     that can be solved by string-matching the answer into the raw text
     isn't diagnosis.
  2. raw_decline_message MUST be drawn from a separate RNG stream than
     decline_code/amount/merchant/halted_days_ago/simulated_customer_response
     - this project actually shipped this bug once during development
     (regenerating the dataset after adding raw_decline_message silently
     changed every downstream record's ground-truth fields for the same
     seed, since it was drawn from the shared stream) and fixing it is
     what this test guards against regressing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from decline_codes import DECLINE_CODES
from generate_data import CODE_WEIGHTS, RAW_SIGNAL_TEMPLATES, generate


def test_every_weighted_code_is_a_real_decline_code():
    for code in CODE_WEIGHTS:
        assert code in DECLINE_CODES, f"{code} in CODE_WEIGHTS is not a real decline code"


def test_every_decline_code_has_raw_signal_templates():
    for code in DECLINE_CODES:
        assert code in RAW_SIGNAL_TEMPLATES, f"{code} has no RAW_SIGNAL_TEMPLATES entry"
        assert len(RAW_SIGNAL_TEMPLATES[code]) >= 1


def test_no_raw_template_leaks_the_decline_code_string():
    # A raw message containing the literal code (e.g. "insufficient_funds")
    # would let diagnosis solve the task by string-matching the answer,
    # not by actually interpreting the message.
    for code, templates in RAW_SIGNAL_TEMPLATES.items():
        for template in templates:
            assert code not in template, (
                f"raw template for {code!r} contains the decline_code string itself: {template!r}"
            )


def test_ambiguous_clusters_share_identical_templates_across_codes_with_different_actions():
    # The whole point of the three deliberate ambiguity clusters: two (or
    # three) codes that map to DIFFERENT recovery actions must draw from
    # the exact same raw-text pool, so diagnosis is genuinely fallible on
    # those codes, not just difficult.
    clusters = [
        ["card_declined", "payment_failed", "payment_risk_check_failed"],
        ["debit_instrument_blocked", "debit_instrument_inactive"],
    ]
    for cluster in clusters:
        pools = [set(RAW_SIGNAL_TEMPLATES[code]) for code in cluster]
        actions = {DECLINE_CODES[code].allowed_action for code in cluster}
        assert len(actions) > 1, f"cluster {cluster} should span more than one recovery action"
        # Every code in the cluster must share at least one identical raw
        # message with every other code in the cluster.
        common = set.intersection(*pools)
        assert common, f"cluster {cluster} has no raw message shared across all its codes"


def test_generated_records_carry_a_raw_decline_message_consistent_with_their_code():
    records = generate(n=50, seed=7)
    for r in records:
        assert r["raw_decline_message"] in RAW_SIGNAL_TEMPLATES[r["decline_code"]]


def test_generation_is_deterministic_given_a_seed():
    a = generate(n=30, seed=99)
    b = generate(n=30, seed=99)
    assert [r["decline_code"] for r in a] == [r["decline_code"] for r in b]
    assert [r["raw_decline_message"] for r in a] == [r["raw_decline_message"] for r in b]
    assert [r["amount_paise"] for r in a] == [r["amount_paise"] for r in b]
    assert [r["halted_days_ago"] for r in a] == [r["halted_days_ago"] for r in b]
    assert [r["simulated_customer_response"] for r in a] == [r["simulated_customer_response"] for r in b]


def test_raw_decline_message_draws_from_an_independent_rng_stream():
    # Regression guard for a real bug hit during development: drawing
    # raw_decline_message from the SAME rng stream as decline_code/amount/
    # merchant/halted_days_ago/simulated_customer_response shifts every
    # subsequent draw for the rest of the batch by one, silently changing
    # which decline_code (and everything else) every later record gets
    # for the SAME seed - this actually happened once while building this
    # feature and corrupted the flagship 150-record dataset's ground truth
    # until caught and fixed (raw_decline_message now draws from its own
    # `raw_signal_rng = random.Random(f"raw-signal-{seed}")`).
    #
    # This is the exact decline_code distribution for generate(seed=42)
    # BEFORE raw_decline_message existed (verified directly against the
    # git-committed data/halted_subscriptions.json). If raw_decline_message
    # is ever accidentally drawn from the main `rng` stream again, this
    # distribution changes and this test fails.
    expected_distribution = {
        "insufficient_funds": 41,
        "card_expired": 26,
        "authentication_failed": 17,
        "card_declined": 11,
        "gateway_technical_error": 8,
        "incorrect_cvv": 8,
        "card_disabled_for_online_payments": 7,
        "bank_technical_error": 7,
        "payment_timed_out": 5,
        "transaction_limit_exceeded": 5,
        "payment_failed": 5,
        "payment_cancelled": 5,
        "debit_instrument_inactive": 3,
        "payment_risk_check_failed": 1,
        "debit_instrument_blocked": 1,
    }
    from collections import Counter
    records = generate(n=150, seed=42)
    actual_distribution = dict(Counter(r["decline_code"] for r in records))
    assert actual_distribution == expected_distribution


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
