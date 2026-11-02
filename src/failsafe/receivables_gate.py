from dataclasses import dataclass

from failsafe.base_gate import (
    MAX_ACTION_AMOUNT_PAISE,
    MAX_RUN_TOTAL_PAISE,
    SpendingCapMixin,
)
from failsafe.gate import MAX_ACTION_AMOUNT_PAISE as GATE_MAX_ACTION
from failsafe.gate import MAX_RUN_TOTAL_PAISE as GATE_MAX_RUN
from failsafe.receivables_policy import (
    NO_ACTION_POLICY_ACTIONS,
    ReceivableAction,
    get_receivable_policy,
)

MAX_REMINDERS_BEFORE_ESCALATION = 4
DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD = 90


@dataclass
class ReceivableGateDecision:
    execute: bool
    reason: str
    final_action: ReceivableAction


class ReceivableGate(SpendingCapMixin):
    def __init__(self):
        self._seen: set[tuple[str, str]] = set()
        self._run_total_paise = 0

    def seed_from_checkpoint(self, already_spent_paise: int, seen_keys: set[tuple[str, str]]):
        self._run_total_paise = already_spent_paise
        self._seen |= seen_keys

    def evaluate(
        self,
        invoice_id: str,
        case_reason: str,
        amount_paise: int,
        days_overdue: int,
        reminders_sent_count: int,
    ) -> ReceivableGateDecision:
        policy = get_receivable_policy(case_reason)
        final_action = policy.allowed_action

        if final_action in NO_ACTION_POLICY_ACTIONS:
            return ReceivableGateDecision(
                execute=True,
                reason=f"No-automated-action policy for '{case_reason}'.",
                final_action=final_action,
            )

        if reminders_sent_count >= MAX_REMINDERS_BEFORE_ESCALATION:
            return ReceivableGateDecision(
                execute=True,
                reason=f"Escalated: {reminders_sent_count} reminders already sent for {invoice_id}, at or past the compliant-escalation cap of {MAX_REMINDERS_BEFORE_ESCALATION} - handing to a human instead of sending another automated reminder.",
                final_action=ReceivableAction.NO_ACTION_ALREADY_ESCALATED,
            )

        if days_overdue >= DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD:
            return ReceivableGateDecision(
                execute=True,
                reason=f"Escalated: invoice {invoice_id} is {days_overdue} days overdue, at or past the {DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD}-day threshold for automated collections - needs a human/legal review instead.",
                final_action=ReceivableAction.NO_ACTION_STALE_INVOICE_NEEDS_LEGAL_REVIEW,
            )

        ok, reason = self._enforce_cap(amount_paise)
        if not ok:
            return ReceivableGateDecision(
                execute=False,
                reason=reason,
                final_action=ReceivableAction.NO_ACTION_NEEDS_HUMAN_REVIEW,
            )

        key = (invoice_id, final_action.value)
        if self._check_idempotency(key):
            return ReceivableGateDecision(
                execute=False,
                reason=f"Duplicate action for {invoice_id} - already processed this run.",
                final_action=ReceivableAction.NO_ACTION_NEEDS_HUMAN_REVIEW,
            )

        self._record(key, amount_paise)
        return ReceivableGateDecision(
            execute=True,
            reason="Passed reminder-cap, staleness, spending cap, and idempotency checks.",
            final_action=final_action,
        )
