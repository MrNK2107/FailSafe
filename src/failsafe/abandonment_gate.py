from dataclasses import dataclass

from failsafe.base_gate import (
    MAX_ACTION_AMOUNT_PAISE,
    MAX_RUN_TOTAL_PAISE,
    SpendingCapMixin,
)
from failsafe.checkout_abandonment_policy import AbandonmentAction, get_abandonment_policy
from failsafe.gate import MAX_ACTION_AMOUNT_PAISE as GATE_MAX_ACTION
from failsafe.gate import MAX_RUN_TOTAL_PAISE as GATE_MAX_RUN

MIN_CART_VALUE_FOR_ACTION_PAISE = 149 * 100
STALE_ABANDONMENT_MINUTES_THRESHOLD = 720


@dataclass
class AbandonmentGateDecision:
    execute: bool
    reason: str
    final_action: AbandonmentAction


class AbandonmentGate(SpendingCapMixin):
    def __init__(self):
        self._seen: set[tuple[str, str]] = set()
        self._run_total_paise = 0

    def seed_from_checkpoint(self, already_spent_paise: int, seen_keys: set[tuple[str, str]]):
        self._run_total_paise = already_spent_paise
        self._seen |= seen_keys

    def evaluate(
        self,
        cart_id: str,
        abandonment_reason: str,
        amount_paise: int,
        minutes_since_abandonment: int,
    ) -> AbandonmentGateDecision:
        policy = get_abandonment_policy(abandonment_reason)
        final_action = policy.allowed_action

        if final_action == AbandonmentAction.NO_ACTION_RESPECT_HESITATION:
            return AbandonmentGateDecision(
                execute=True,
                reason=f"No-action policy for '{abandonment_reason}'.",
                final_action=final_action,
            )

        if minutes_since_abandonment >= STALE_ABANDONMENT_MINUTES_THRESHOLD:
            return AbandonmentGateDecision(
                execute=True,
                reason=f"Escalated/skipped: abandoned {minutes_since_abandonment} minutes ago, at or past the {STALE_ABANDONMENT_MINUTES_THRESHOLD}-minute staleness threshold for an automated nudge.",
                final_action=AbandonmentAction.NO_ACTION_STALE_ABANDONMENT,
            )

        if amount_paise < MIN_CART_VALUE_FOR_ACTION_PAISE:
            return AbandonmentGateDecision(
                execute=True,
                reason=f"Cart amount {amount_paise / 100:.2f} is below the {MIN_CART_VALUE_FOR_ACTION_PAISE / 100:.2f} minimum for an automated nudge.",
                final_action=AbandonmentAction.NO_ACTION_LOW_VALUE,
            )

        ok, reason = self._enforce_cap(amount_paise)
        if not ok:
            return AbandonmentGateDecision(
                execute=False,
                reason=reason,
                final_action=AbandonmentAction.NO_ACTION_NEEDS_HUMAN_REVIEW,
            )

        key = (cart_id, final_action.value)
        if self._check_idempotency(key):
            return AbandonmentGateDecision(
                execute=False,
                reason=f"Duplicate action for {cart_id} - already processed this run.",
                final_action=AbandonmentAction.NO_ACTION_NEEDS_HUMAN_REVIEW,
            )

        self._record(key, amount_paise)
        return AbandonmentGateDecision(
            execute=True,
            reason="Passed value floor, staleness, spending cap, and idempotency checks.",
            final_action=final_action,
        )
