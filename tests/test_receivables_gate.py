"""
Unit tests for receivables_gate.py - mirrors tests/test_gate.py's and
tests/test_abandonment_gate.py's own discipline: this must be correct
independent of any LLM behaving correctly, since that is the entire point
of a deterministic enforcement layer. Also the load-bearing test suite for
this domain's own "compliant escalation + stopping rules" bar
(MAX_REMINDERS_BEFORE_ESCALATION, DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gate import MAX_ACTION_AMOUNT_PAISE, MAX_RUN_TOTAL_PAISE
from receivables_gate import (
    DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD,
    MAX_REMINDERS_BEFORE_ESCALATION,
    ReceivableGate,
)
from receivables_policy import ReceivableAction


def test_gate_executes_correct_policy_action():
    gate = ReceivableGate()
    decision = gate.evaluate("inv_1", "payment_process_friction", 45000, days_overdue=3, reminders_sent_count=0)
    assert decision.execute
    assert decision.final_action == ReceivableAction.FRIENDLY_REMINDER


def test_gate_executes_no_action_policy_for_dispute():
    gate = ReceivableGate()
    decision = gate.evaluate("inv_2", "invoice_dispute_likely", 45000, days_overdue=20, reminders_sent_count=2)
    assert decision.execute  # "executing" a no-action policy just means refusing
    assert decision.final_action == ReceivableAction.NO_ACTION_NEEDS_DISPUTE_REVIEW


def test_gate_executes_no_action_policy_for_high_risk_escalation():
    gate = ReceivableGate()
    decision = gate.evaluate("inv_3", "high_risk_non_payment", 45000, days_overdue=40, reminders_sent_count=2)
    assert decision.execute
    assert decision.final_action == ReceivableAction.ESCALATE_TO_MANUAL_COLLECTIONS


def test_no_action_policy_bypasses_reminder_cap_and_staleness_rules():
    # A policy-mandated no-action is already the safest outcome - passing
    # a reminder count AND a days_overdue past both stopping-rule
    # thresholds must not change it or need those rules to fire for it to
    # be handled correctly.
    gate = ReceivableGate()
    decision = gate.evaluate(
        "inv_4", "invoice_dispute_likely", 100,
        days_overdue=DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD + 50,
        reminders_sent_count=MAX_REMINDERS_BEFORE_ESCALATION + 5,
    )
    assert decision.final_action == ReceivableAction.NO_ACTION_NEEDS_DISPUTE_REVIEW


def test_gate_escalates_at_reminder_cap_instead_of_sending_another_reminder():
    gate = ReceivableGate()
    decision = gate.evaluate(
        "inv_5", "cash_flow_delay", 45000, days_overdue=20,
        reminders_sent_count=MAX_REMINDERS_BEFORE_ESCALATION,
    )
    assert decision.execute
    assert decision.final_action == ReceivableAction.NO_ACTION_ALREADY_ESCALATED
    assert "escalation cap" in decision.reason.lower() or "escalated" in decision.reason.lower()


def test_gate_does_not_escalate_just_under_the_reminder_cap():
    gate = ReceivableGate()
    decision = gate.evaluate(
        "inv_6", "cash_flow_delay", 45000, days_overdue=20,
        reminders_sent_count=MAX_REMINDERS_BEFORE_ESCALATION - 1,
    )
    assert decision.execute
    assert decision.final_action == ReceivableAction.PAYMENT_PLAN_OFFER


def test_gate_escalates_stale_invoice_to_legal_review():
    gate = ReceivableGate()
    decision = gate.evaluate(
        "inv_7", "chronic_late_payer_will_eventually_pay", 45000,
        days_overdue=DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD, reminders_sent_count=1,
    )
    assert decision.execute
    assert decision.final_action == ReceivableAction.NO_ACTION_STALE_INVOICE_NEEDS_LEGAL_REVIEW


def test_gate_does_not_escalate_invoice_just_under_the_staleness_threshold():
    gate = ReceivableGate()
    decision = gate.evaluate(
        "inv_8", "chronic_late_payer_will_eventually_pay", 45000,
        days_overdue=DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD - 1, reminders_sent_count=1,
    )
    assert decision.execute
    assert decision.final_action == ReceivableAction.FIRM_REMINDER_WITH_DEADLINE


def test_gate_hard_blocks_amount_over_cap():
    gate = ReceivableGate()
    decision = gate.evaluate(
        "inv_9", "cash_flow_delay", MAX_ACTION_AMOUNT_PAISE + 1, days_overdue=3, reminders_sent_count=0,
    )
    assert not decision.execute
    assert decision.final_action == ReceivableAction.NO_ACTION_NEEDS_HUMAN_REVIEW


def test_gate_hard_blocks_duplicate_action_same_run():
    gate = ReceivableGate()
    first = gate.evaluate("inv_10", "payment_process_friction", 45000, days_overdue=3, reminders_sent_count=0)
    second = gate.evaluate("inv_10", "payment_process_friction", 45000, days_overdue=3, reminders_sent_count=0)
    assert first.execute
    assert not second.execute
    assert second.final_action == ReceivableAction.NO_ACTION_NEEDS_HUMAN_REVIEW


def test_gate_hard_blocks_when_run_total_cap_would_be_exceeded():
    # Regression test: this gate tracked self._run_total_paise from the
    # start but never actually compared it against MAX_RUN_TOTAL_PAISE
    # (found on a later code review, the same gap abandonment_gate.py had).
    # Directly seeding the running total mirrors how a real run accumulates
    # it across many in-cap actions, without needing 10+ real evaluate() calls.
    gate = ReceivableGate()
    gate._run_total_paise = MAX_RUN_TOTAL_PAISE - 100
    decision = gate.evaluate("inv_13", "payment_process_friction", 45000, days_overdue=3, reminders_sent_count=0)
    assert not decision.execute
    assert decision.final_action == ReceivableAction.NO_ACTION_NEEDS_HUMAN_REVIEW
    assert "run-total" in decision.reason.lower()


def test_gate_does_not_block_when_run_total_cap_would_not_be_exceeded():
    gate = ReceivableGate()
    gate._run_total_paise = MAX_RUN_TOTAL_PAISE - 100000
    decision = gate.evaluate("inv_14", "payment_process_friction", 45000, days_overdue=3, reminders_sent_count=0)
    assert decision.execute


def test_gate_reuses_the_real_gate_py_cap_constant_not_a_duplicated_number():
    # Structural guard against exactly the anti-pattern this module's own
    # docstring promises to avoid: a hardcoded, silently-drifting copy of
    # gate.py's spending cap.
    gate = ReceivableGate()
    just_under = gate.evaluate("inv_11", "cash_flow_delay", MAX_ACTION_AMOUNT_PAISE, days_overdue=3, reminders_sent_count=0)
    assert just_under.execute
    just_over = gate.evaluate("inv_12", "cash_flow_delay", MAX_ACTION_AMOUNT_PAISE + 1, days_overdue=3, reminders_sent_count=0)
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
