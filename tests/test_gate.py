"""
Unit tests for the gate - the one file in this project that must be
trustworthy independent of any LLM behaving correctly.
Run with: .venv/Scripts/python.exe -m pytest tests/ -v
(or just: .venv/Scripts/python.exe tests/test_gate.py)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from decline_codes import RecoveryAction
from gate import MAX_ACTION_AMOUNT_PAISE, MAX_ATTEMPTS_PER_SUBSCRIPTION, STALE_HALT_ESCALATION_DAYS, Gate


def test_gate_executes_correct_policy_action():
    gate = Gate()
    decision = gate.evaluate("sub_1", "insufficient_funds", RecoveryAction.DELAYED_RETRY, 29900)
    assert decision.execute
    assert decision.llm_matched_policy
    assert decision.final_action == RecoveryAction.DELAYED_RETRY


def test_gate_overrides_llm_but_still_executes_corrected_action():
    gate = Gate()
    # insufficient_funds policy is DELAYED_RETRY, not IMMEDIATE_RETRY
    decision = gate.evaluate("sub_2", "insufficient_funds", RecoveryAction.IMMEDIATE_RETRY, 29900)
    assert not decision.llm_matched_policy
    assert decision.execute  # override, not a block - the corrected action still runs
    assert decision.final_action == RecoveryAction.DELAYED_RETRY


def test_gate_never_allows_retry_on_fraud_even_if_llm_proposes_it():
    gate = Gate()
    decision = gate.evaluate("sub_3", "payment_risk_check_failed", RecoveryAction.IMMEDIATE_RETRY, 29900)
    assert not decision.llm_matched_policy
    assert decision.final_action == RecoveryAction.NO_ACTION_FRAUD
    assert decision.execute  # "executing" a no-action policy just means refusing


def test_gate_respects_fraud_policy_when_llm_gets_it_right():
    gate = Gate()
    decision = gate.evaluate("sub_4", "payment_risk_check_failed", RecoveryAction.NO_ACTION_FRAUD, 29900)
    assert decision.llm_matched_policy
    assert decision.final_action == RecoveryAction.NO_ACTION_FRAUD


def test_gate_hard_blocks_amount_over_cap():
    gate = Gate()
    decision = gate.evaluate(
        "sub_5", "insufficient_funds", RecoveryAction.DELAYED_RETRY, MAX_ACTION_AMOUNT_PAISE + 1
    )
    assert not decision.execute
    assert decision.final_action == RecoveryAction.NO_ACTION_UNRECOVERABLE


def test_gate_hard_blocks_duplicate_action_same_run():
    gate = Gate()
    first = gate.evaluate("sub_6", "insufficient_funds", RecoveryAction.DELAYED_RETRY, 29900)
    second = gate.evaluate("sub_6", "insufficient_funds", RecoveryAction.DELAYED_RETRY, 29900)
    assert first.execute
    assert not second.execute


def test_gate_executes_unrecoverable_no_action_policy_even_if_llm_disagrees():
    # D3 (BUILD_LOG §7.3): a policy-mandated no-action (customer cancelled,
    # instrument permanently blocked) must still "execute" (i.e. correctly
    # do nothing) regardless of what the LLM proposed, and must never be
    # confused with a hard block (execute=False) - refusing IS the action.
    gate = Gate()
    decision = gate.evaluate("sub_7", "payment_cancelled", RecoveryAction.DELAYED_RETRY, 29900)
    assert not decision.llm_matched_policy
    assert decision.execute
    assert decision.final_action == RecoveryAction.NO_ACTION_UNRECOVERABLE


def test_gate_escalates_after_cross_run_attempt_cap_reached():
    # Idempotency only stops a duplicate action within one run - this proves
    # the separate cross-run stopping rule fires once real attempts pile up
    # across previous runs, even though this is fresh Gate (i.e. it isn't
    # relying on same-run idempotency to catch it).
    gate = Gate()
    decision = gate.evaluate(
        "sub_8", "insufficient_funds", RecoveryAction.DELAYED_RETRY, 29900,
        prior_attempt_count=MAX_ATTEMPTS_PER_SUBSCRIPTION,
    )
    assert decision.execute
    assert decision.final_action == RecoveryAction.NO_ACTION_UNRECOVERABLE
    assert "Escalated to manual review" in decision.reason
    assert "prior recovery" in decision.reason


def test_gate_does_not_escalate_below_attempt_cap():
    gate = Gate()
    decision = gate.evaluate(
        "sub_9", "insufficient_funds", RecoveryAction.DELAYED_RETRY, 29900,
        prior_attempt_count=MAX_ATTEMPTS_PER_SUBSCRIPTION - 1,
    )
    assert decision.final_action == RecoveryAction.DELAYED_RETRY
    assert decision.execute


def test_gate_escalates_stale_halted_subscription_instead_of_nudging():
    gate = Gate()
    decision = gate.evaluate(
        "sub_10", "card_expired", RecoveryAction.PAYMENT_LINK_NUDGE, 29900,
        halted_days_ago=STALE_HALT_ESCALATION_DAYS,
    )
    assert decision.execute
    assert decision.final_action == RecoveryAction.NO_ACTION_UNRECOVERABLE
    assert "staleness threshold" in decision.reason


def test_gate_does_not_escalate_fresh_halted_subscription():
    gate = Gate()
    decision = gate.evaluate(
        "sub_11", "card_expired", RecoveryAction.PAYMENT_LINK_NUDGE, 29900,
        halted_days_ago=STALE_HALT_ESCALATION_DAYS - 1,
    )
    assert decision.final_action == RecoveryAction.PAYMENT_LINK_NUDGE
    assert decision.execute


def test_gate_ignores_missing_halted_days_ago():
    # agent_onetime.py's one-time payments have no halt clock at all - the
    # staleness rule must simply never apply, not error out.
    gate = Gate()
    decision = gate.evaluate(
        "sub_12", "card_expired", RecoveryAction.PAYMENT_LINK_NUDGE, 29900,
        halted_days_ago=None,
    )
    assert decision.final_action == RecoveryAction.PAYMENT_LINK_NUDGE
    assert decision.execute


def test_gate_no_action_policy_bypasses_escalation_rules():
    # A policy-mandated no-action (fraud) is already the safest outcome -
    # the escalation rules must not need to fire for it to be handled
    # correctly, and passing a maxed-out attempt count must not change it.
    gate = Gate()
    decision = gate.evaluate(
        "sub_13", "payment_risk_check_failed", RecoveryAction.IMMEDIATE_RETRY, 29900,
        prior_attempt_count=MAX_ATTEMPTS_PER_SUBSCRIPTION,
    )
    assert decision.final_action == RecoveryAction.NO_ACTION_FRAUD


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
