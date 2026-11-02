import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from failsafe.paths import CONFIG_DIR

POLICY_PATH = CONFIG_DIR / "receivables_policy.json"


class ReceivableReason(str, Enum):
    CASH_FLOW_DELAY = "cash_flow_delay"
    PAYMENT_PROCESS_FRICTION = "payment_process_friction"
    CHRONIC_LATE_PAYER_WILL_EVENTUALLY_PAY = "chronic_late_payer_will_eventually_pay"
    INVOICE_DISPUTE_LIKELY = "invoice_dispute_likely"
    HIGH_RISK_NON_PAYMENT = "high_risk_non_payment"


class ReceivableAction(str, Enum):
    FRIENDLY_REMINDER = "friendly_reminder"
    PAYMENT_PLAN_OFFER = "payment_plan_offer"
    FIRM_REMINDER_WITH_DEADLINE = "firm_reminder_with_deadline"
    NO_ACTION_NEEDS_DISPUTE_REVIEW = "no_action_needs_dispute_review"
    ESCALATE_TO_MANUAL_COLLECTIONS = "escalate_to_manual_collections"
    NO_ACTION_ALREADY_ESCALATED = "no_action_already_escalated"
    NO_ACTION_STALE_INVOICE_NEEDS_LEGAL_REVIEW = "no_action_stale_invoice_needs_legal_review"
    NO_ACTION_NEEDS_HUMAN_REVIEW = "no_action_needs_human_review"


GATE_ONLY_ACTIONS = {
    ReceivableAction.NO_ACTION_ALREADY_ESCALATED,
    ReceivableAction.NO_ACTION_STALE_INVOICE_NEEDS_LEGAL_REVIEW,
    ReceivableAction.NO_ACTION_NEEDS_HUMAN_REVIEW,
}

NO_ACTION_POLICY_ACTIONS = {
    ReceivableAction.NO_ACTION_NEEDS_DISPUTE_REVIEW,
    ReceivableAction.ESCALATE_TO_MANUAL_COLLECTIONS,
}


@dataclass(frozen=True)
class ReceivablePolicy:
    reason: str
    description: str
    allowed_action: ReceivableAction
    simulated_recovery_rate: float


def _load_receivables_policy(path: Path) -> dict[str, ReceivablePolicy]:
    from failsafe.policy_loader import load_policy

    validated = load_policy(path, ReceivableReason, ReceivableAction)
    policies = {}
    for reason, entry in validated.items():
        policies[reason] = ReceivablePolicy(
            reason=ReceivableReason(reason).value,
            description=entry["description"],
            allowed_action=ReceivableAction(entry["allowed_action"]),
            simulated_recovery_rate=float(entry["simulated_recovery_rate"]),
        )
    return policies


RECEIVABLE_POLICIES: dict[str, ReceivablePolicy] = _load_receivables_policy(POLICY_PATH)


def get_receivable_policy(reason: str) -> ReceivablePolicy:
    if reason not in RECEIVABLE_POLICIES:
        raise KeyError(f"Unknown receivable case reason: {reason!r}")
    return RECEIVABLE_POLICIES[reason]
