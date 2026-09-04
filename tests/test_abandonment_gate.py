"""
Unit tests for abandonment_gate.py - mirrors tests/test_gate.py's own
discipline: this must be correct independent of any LLM behaving
correctly, since that is the entire point of a deterministic enforcement
layer.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from abandonment_gate import (
    MIN_CART_VALUE_FOR_ACTION_PAISE,
    STALE_ABANDONMENT_MINUTES_THRESHOLD,
    AbandonmentGate,
)
from checkout_abandonment_policy import AbandonmentAction
from gate import MAX_ACTION_AMOUNT_PAISE, MAX_RUN_TOTAL_PAISE


def test_gate_executes_correct_policy_action():
    gate = AbandonmentGate()
    decision = gate.evaluate("cart_1", "otp_delay_or_failure", 29900, minutes_since_abandonment=3)
    assert decision.execute
    assert decision.final_action == AbandonmentAction.IMMEDIATE_PAYMENT_LINK_RESEND


def test_gate_executes_no_action_policy_for_trust_concern():
    gate = AbandonmentGate()
    decision = gate.evaluate("cart_2", "trust_or_security_concern", 29900, minutes_since_abandonment=3)
    assert decision.execute  # "executing" a no-action policy just means refusing
    assert decision.final_action == AbandonmentAction.NO_ACTION_RESPECT_HESITATION


def test_no_action_policy_bypasses_value_and_staleness_rules():
    # A policy-mandated no-action is already the safest outcome - passing
    # a stale timestamp AND a tiny amount must not change it or need
    # those rules to fire for it to be handled correctly.
    gate = AbandonmentGate()
    decision = gate.evaluate(
        "cart_3", "trust_or_security_concern", 100,
        minutes_since_abandonment=STALE_ABANDONMENT_MINUTES_THRESHOLD + 100,
    )
    assert decision.final_action == AbandonmentAction.NO_ACTION_RESPECT_HESITATION


def test_gate_escalates_stale_abandonment_instead_of_nudging():
    gate = AbandonmentGate()
    decision = gate.evaluate(
        "cart_4", "price_shock", 29900,
        minutes_since_abandonment=STALE_ABANDONMENT_MINUTES_THRESHOLD,
    )
    assert decision.execute
    assert decision.final_action == AbandonmentAction.NO_ACTION_STALE_ABANDONMENT
    assert "staleness" in decision.reason


def test_gate_does_not_escalate_fresh_abandonment():
    gate = AbandonmentGate()
    decision = gate.evaluate(
        "cart_5", "price_shock", 29900,
        minutes_since_abandonment=STALE_ABANDONMENT_MINUTES_THRESHOLD - 1,
    )
    assert decision.final_action == AbandonmentAction.DISCOUNTED_INCENTIVE_NUDGE
    assert decision.execute


def test_gate_skips_low_value_cart():
    gate = AbandonmentGate()
    decision = gate.evaluate(
        "cart_6", "otp_delay_or_failure", MIN_CART_VALUE_FOR_ACTION_PAISE - 1,
        minutes_since_abandonment=3,
    )
    assert decision.execute
    assert decision.final_action == AbandonmentAction.NO_ACTION_LOW_VALUE


def test_gate_does_not_skip_cart_at_or_above_value_floor():
    gate = AbandonmentGate()
    decision = gate.evaluate(
        "cart_7", "otp_delay_or_failure", MIN_CART_VALUE_FOR_ACTION_PAISE,
        minutes_since_abandonment=3,
    )
    assert decision.final_action == AbandonmentAction.IMMEDIATE_PAYMENT_LINK_RESEND
    assert decision.execute


def test_gate_hard_blocks_amount_over_cap():
    gate = AbandonmentGate()
    decision = gate.evaluate(
        "cart_8", "price_shock", MAX_ACTION_AMOUNT_PAISE + 1, minutes_since_abandonment=3,
    )
    assert not decision.execute
    assert decision.final_action == AbandonmentAction.NO_ACTION_NEEDS_HUMAN_REVIEW


def test_gate_hard_blocks_duplicate_action_same_run():
    gate = AbandonmentGate()
    first = gate.evaluate("cart_9", "otp_delay_or_failure", 29900, minutes_since_abandonment=3)
    second = gate.evaluate("cart_9", "otp_delay_or_failure", 29900, minutes_since_abandonment=3)
    assert first.execute
    assert not second.execute
    assert second.final_action == AbandonmentAction.NO_ACTION_NEEDS_HUMAN_REVIEW


def test_gate_hard_blocks_when_run_total_cap_would_be_exceeded():
    # Regression test: this gate tracked self._run_total_paise from the
    # start but never actually compared it against MAX_RUN_TOTAL_PAISE
    # (found on a later code review - the per-action cap and idempotency
    # checks were enforced, the run-total one silently wasn't). Directly
    # seeding the running total mirrors how a real run accumulates it
    # across many in-cap actions, without needing 10+ real evaluate() calls.
    gate = AbandonmentGate()
    gate._run_total_paise = MAX_RUN_TOTAL_PAISE - 100
    decision = gate.evaluate("cart_12", "otp_delay_or_failure", 29900, minutes_since_abandonment=3)
    assert not decision.execute
    assert decision.final_action == AbandonmentAction.NO_ACTION_NEEDS_HUMAN_REVIEW
    assert "run-total" in decision.reason.lower()


def test_gate_does_not_block_when_run_total_cap_would_not_be_exceeded():
    gate = AbandonmentGate()
    gate._run_total_paise = MAX_RUN_TOTAL_PAISE - 100000
    decision = gate.evaluate("cart_13", "otp_delay_or_failure", 29900, minutes_since_abandonment=3)
    assert decision.execute


def test_gate_reuses_the_real_gate_py_cap_constant_not_a_duplicated_number():
    # Structural guard against exactly the anti-pattern this module's own
    # docstring promises to avoid: a hardcoded, silently-drifting copy of
    # gate.py's spending cap.
    gate = AbandonmentGate()
    just_under = gate.evaluate("cart_10", "price_shock", MAX_ACTION_AMOUNT_PAISE, minutes_since_abandonment=3)
    assert just_under.execute
    just_over = gate.evaluate("cart_11", "price_shock", MAX_ACTION_AMOUNT_PAISE + 1, minutes_since_abandonment=3)
    assert not just_over.execute


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
