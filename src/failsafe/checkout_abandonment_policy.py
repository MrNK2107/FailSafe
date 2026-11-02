import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from failsafe.paths import CONFIG_DIR

POLICY_PATH = CONFIG_DIR / "abandonment_policy.json"


class AbandonmentReason(str, Enum):
    OTP_DELAY_OR_FAILURE = "otp_delay_or_failure"
    PAYMENT_METHOD_UNSUPPORTED = "payment_method_unsupported"
    PRICE_SHOCK = "price_shock"
    DISTRACTION_OR_MULTITASKING = "distraction_or_multitasking"
    TRUST_OR_SECURITY_CONCERN = "trust_or_security_concern"


class AbandonmentAction(str, Enum):
    IMMEDIATE_PAYMENT_LINK_RESEND = "immediate_payment_link_resend"
    PAYMENT_LINK_ALTERNATE_METHODS_NUDGE = "payment_link_alternate_methods_nudge"
    DISCOUNTED_INCENTIVE_NUDGE = "discounted_incentive_nudge"
    DELAYED_NUDGE_NO_DISCOUNT = "delayed_nudge_no_discount"
    NO_ACTION_RESPECT_HESITATION = "no_action_respect_hesitation"
    NO_ACTION_LOW_VALUE = "no_action_low_value"
    NO_ACTION_STALE_ABANDONMENT = "no_action_stale_abandonment"
    NO_ACTION_NEEDS_HUMAN_REVIEW = "no_action_needs_human_review"


GATE_ONLY_ACTIONS = {
    AbandonmentAction.NO_ACTION_LOW_VALUE,
    AbandonmentAction.NO_ACTION_STALE_ABANDONMENT,
    AbandonmentAction.NO_ACTION_NEEDS_HUMAN_REVIEW,
}


@dataclass(frozen=True)
class AbandonmentPolicy:
    reason: str
    description: str
    allowed_action: AbandonmentAction
    simulated_recovery_rate: float


def _load_abandonment_policy(path: Path) -> dict[str, AbandonmentPolicy]:
    from failsafe.policy_loader import load_policy

    validated = load_policy(path, AbandonmentReason, AbandonmentAction)
    policies = {}
    for reason, entry in validated.items():
        policies[reason] = AbandonmentPolicy(
            reason=AbandonmentReason(reason).value,
            description=entry["description"],
            allowed_action=AbandonmentAction(entry["allowed_action"]),
            simulated_recovery_rate=float(entry["simulated_recovery_rate"]),
        )
    return policies


ABANDONMENT_POLICIES: dict[str, AbandonmentPolicy] = _load_abandonment_policy(POLICY_PATH)


def get_abandonment_policy(reason: str) -> AbandonmentPolicy:
    if reason not in ABANDONMENT_POLICIES:
        raise KeyError(f"Unknown abandonment reason: {reason!r}")
    return ABANDONMENT_POLICIES[reason]
