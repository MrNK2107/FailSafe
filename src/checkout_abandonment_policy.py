"""
Policy table for CHECKOUT ABANDONMENT - the second revenue-loss category
named in the Track 3 problem statement ("payment failures and checkout
abandonment... to overdue receivables"), and, before this file existed,
entirely unimplemented (PS_REQUIREMENTS_DEBATE.md's category-scope
finding, and README.md §6's own honest disclosure of it). See
BUILD_LOG.md's dated checkout-abandonment section for the full writeup.

Structurally different from decline_codes.py/config/decline_policy.json
by definition, not by choice: a checkout-abandonment record has NO
decline_code, because no payment was ever attempted or declined - the
customer left before completing a checkout attempt at all. So this module
is a parallel, not a reuse: its own enum pair (AbandonmentReason,
AbandonmentAction), its own JSON policy file (config/abandonment_policy.json),
and its own loader/validator - but mirrors decline_codes.py's exact shape
and the same "fail loudly on a typo" discipline, since a merchant editing
this file by hand deserves the same protection.

AbandonmentAction has 8 values, not 5, because this enforcement layer
(abandonment_gate.py) distinguishes MORE no-action reasons than gate.py
does, on purpose - see that module's docstring:
  - 5 are reason-driven (one per AbandonmentReason, exactly like
    decline_codes.py's one-code-to-one-action mapping) and appear as an
    `allowed_action` value in this file.
  - 3 (NO_ACTION_LOW_VALUE, NO_ACTION_STALE_ABANDONMENT,
    NO_ACTION_NEEDS_HUMAN_REVIEW) are enforcement-layer-only fallbacks,
    never a per-reason `allowed_action` in this file, produced only by
    abandonment_gate.py's own stopping rules (value floor, staleness,
    spending cap/idempotency hard-block) - mirroring how gate.py's
    NO_ACTION_UNRECOVERABLE already does double duty as both a real
    per-code policy AND its own hard-block fallback action. All 8 still
    need a plain-English glossary entry (`_action_glossary` in
    config/abandonment_policy.json) - test_every_action_has_a_glossary_entry
    enforces this exactly like decline_codes.py's own glossary test does.
"""

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

POLICY_PATH = Path(__file__).parent.parent / "config" / "abandonment_policy.json"


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
    # Enforcement-layer-only fallbacks - never a per-reason allowed_action
    # in config/abandonment_policy.json, only ever produced by
    # abandonment_gate.py.
    NO_ACTION_LOW_VALUE = "no_action_low_value"
    NO_ACTION_STALE_ABANDONMENT = "no_action_stale_abandonment"
    NO_ACTION_NEEDS_HUMAN_REVIEW = "no_action_needs_human_review"


# Fallback actions produced only by the gate, never by a policy row -
# used by the glossary-completeness test to explain why these 3 have no
# corresponding reason row.
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
    # Rough probability a nudge actually recovers the cart, used ONLY by
    # generate_checkout_abandonment_data.py's synthetic simulator - a
    # labeled assumption, not a measured real-world rate, exactly like
    # decline_codes.py's simulated_success_rate.
    simulated_recovery_rate: float


def _load_abandonment_policy(path: Path) -> dict[str, AbandonmentPolicy]:
    """
    Loads and validates config/abandonment_policy.json. Fails loudly and
    specifically (which reason, which field, why) on a bad edit - mirrors
    decline_codes.py's _load_decline_codes exactly, same reasoning: the
    gate trusts this table as ground truth and must never silently enforce
    a broken policy.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    policies = {}
    for reason, entry in raw.items():
        if reason.startswith("_"):
            continue
        try:
            reason_enum = AbandonmentReason(reason)
        except ValueError:
            raise ValueError(
                f"{path.name}: {reason!r} is not a known AbandonmentReason; "
                f"must be one of {[r.value for r in AbandonmentReason]}"
            )
        try:
            allowed_action = AbandonmentAction(entry["allowed_action"])
        except ValueError:
            raise ValueError(
                f"{path.name}: {reason!r} has invalid allowed_action {entry['allowed_action']!r}; "
                f"must be one of {[a.value for a in AbandonmentAction]}"
            )
        rate = entry["simulated_recovery_rate"]
        if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not (0.0 <= rate <= 1.0):
            raise ValueError(
                f"{path.name}: {reason!r} has invalid simulated_recovery_rate {rate!r}; "
                f"must be a number between 0.0 and 1.0"
            )

        policies[reason] = AbandonmentPolicy(
            reason=reason_enum.value,
            description=entry["description"],
            allowed_action=allowed_action,
            simulated_recovery_rate=float(rate),
        )
    return policies


ABANDONMENT_POLICIES: dict[str, AbandonmentPolicy] = _load_abandonment_policy(POLICY_PATH)


def get_abandonment_policy(reason: str) -> AbandonmentPolicy:
    if reason not in ABANDONMENT_POLICIES:
        raise KeyError(f"Unknown abandonment reason: {reason!r}")
    return ABANDONMENT_POLICIES[reason]
