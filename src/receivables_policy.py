"""
Policy table for OVERDUE RECEIVABLES - the third and final revenue-loss
category named in the Track 3 problem statement ("payment failures and
checkout abandonment... to overdue receivables"), and, before this file
existed, entirely unimplemented (PS_REQUIREMENTS_DEBATE.md's category-scope
finding, and README.md §6's own honest disclosure of it, which explicitly
tracked this as the one remaining gap after checkout abandonment closed the
other one). See BUILD_LOG.md's dated overdue-receivables section for the
full writeup.

Structurally different from every other domain in this repo, not by
choice but by definition: an overdue receivable is a B2B invoice that was
issued, sent, and never paid by its due date - there is no `decline_code`
(nobody attempted or declined a payment) and no checkout funnel (nobody
started a checkout at all). What this domain actually revolves around is
an AGING CLOCK - `days_overdue` - and a business's payment-behavior
history, not a payment event of any kind. So this module is a third
parallel, not a reuse, of decline_codes.py/config/decline_policy.json and
checkout_abandonment_policy.py/config/abandonment_policy.json: its own
enum pair (ReceivableReason, ReceivableAction), its own JSON policy file
(config/receivables_policy.json), and its own loader/validator - but
mirrors both prior files' exact shape and the same "fail loudly on a
typo" discipline, since a collections team editing this file by hand
deserves the same protection a merchant gets for decline_policy.json.

ReceivableAction has 8 values, mirroring AbandonmentAction's own 8-value
shape exactly:
  - 5 are reason-driven (one per ReceivableReason) and appear as an
    `allowed_action` value in config/receivables_policy.json.
  - 3 (NO_ACTION_ALREADY_ESCALATED, NO_ACTION_STALE_INVOICE_NEEDS_LEGAL_REVIEW,
    NO_ACTION_NEEDS_HUMAN_REVIEW) are enforcement-layer-only fallbacks,
    never a per-reason `allowed_action` in this file, produced only by
    receivables_gate.py's own compliant-escalation stopping rules
    (reminder-count cap, staleness threshold) and hard blocks (spending
    cap, idempotency) - mirroring how gate.py's NO_ACTION_UNRECOVERABLE and
    abandonment_gate.py's NO_ACTION_NEEDS_HUMAN_REVIEW already do the same
    double duty. All 8 still require a plain-English glossary entry
    (`_action_glossary` in config/receivables_policy.json).
"""

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

POLICY_PATH = Path(__file__).parent.parent / "config" / "receivables_policy.json"


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
    # Enforcement-layer-only fallbacks - never a per-reason allowed_action
    # in config/receivables_policy.json, only ever produced by
    # receivables_gate.py.
    NO_ACTION_ALREADY_ESCALATED = "no_action_already_escalated"
    NO_ACTION_STALE_INVOICE_NEEDS_LEGAL_REVIEW = "no_action_stale_invoice_needs_legal_review"
    NO_ACTION_NEEDS_HUMAN_REVIEW = "no_action_needs_human_review"


# Fallback actions produced only by the gate, never by a policy row - used
# by the glossary-completeness test to explain why these 3 have no
# corresponding reason row.
GATE_ONLY_ACTIONS = {
    ReceivableAction.NO_ACTION_ALREADY_ESCALATED,
    ReceivableAction.NO_ACTION_STALE_INVOICE_NEEDS_LEGAL_REVIEW,
    ReceivableAction.NO_ACTION_NEEDS_HUMAN_REVIEW,
}

# The one reason-driven action here that is itself a "no automated action,
# hand to a human" outcome - used by receivables_gate.py to decide which
# policy rows bypass the stopping rules/spending cap the way a genuine
# refusal should (mirrors gate.py's NO_ACTION_FRAUD/NO_ACTION_UNRECOVERABLE
# bypass and abandonment_gate.py's NO_ACTION_RESPECT_HESITATION bypass).
NO_ACTION_POLICY_ACTIONS = {
    ReceivableAction.NO_ACTION_NEEDS_DISPUTE_REVIEW,
    ReceivableAction.ESCALATE_TO_MANUAL_COLLECTIONS,
}


@dataclass(frozen=True)
class ReceivablePolicy:
    reason: str
    description: str
    allowed_action: ReceivableAction
    # Rough probability the customer pays after this action, used ONLY by
    # generate_receivables_data.py's synthetic simulator - a labeled
    # assumption, not a measured real-world rate, exactly like
    # decline_codes.py's simulated_success_rate and
    # checkout_abandonment_policy.py's simulated_recovery_rate.
    simulated_recovery_rate: float


def _load_receivables_policy(path: Path) -> dict[str, ReceivablePolicy]:
    """
    Loads and validates config/receivables_policy.json. Fails loudly and
    specifically (which reason, which field, why) on a bad edit - mirrors
    decline_codes.py's/checkout_abandonment_policy.py's own loaders
    exactly, same reasoning: the gate trusts this table as ground truth
    and must never silently enforce a broken policy.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    policies = {}
    for reason, entry in raw.items():
        if reason.startswith("_"):
            continue
        try:
            reason_enum = ReceivableReason(reason)
        except ValueError:
            raise ValueError(
                f"{path.name}: {reason!r} is not a known ReceivableReason; "
                f"must be one of {[r.value for r in ReceivableReason]}"
            )
        try:
            allowed_action = ReceivableAction(entry["allowed_action"])
        except ValueError:
            raise ValueError(
                f"{path.name}: {reason!r} has invalid allowed_action {entry['allowed_action']!r}; "
                f"must be one of {[a.value for a in ReceivableAction]}"
            )
        rate = entry["simulated_recovery_rate"]
        if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not (0.0 <= rate <= 1.0):
            raise ValueError(
                f"{path.name}: {reason!r} has invalid simulated_recovery_rate {rate!r}; "
                f"must be a number between 0.0 and 1.0"
            )

        policies[reason] = ReceivablePolicy(
            reason=reason_enum.value,
            description=entry["description"],
            allowed_action=allowed_action,
            simulated_recovery_rate=float(rate),
        )
    return policies


RECEIVABLE_POLICIES: dict[str, ReceivablePolicy] = _load_receivables_policy(POLICY_PATH)


def get_receivable_policy(reason: str) -> ReceivablePolicy:
    if reason not in RECEIVABLE_POLICIES:
        raise KeyError(f"Unknown receivable case reason: {reason!r}")
    return RECEIVABLE_POLICIES[reason]
