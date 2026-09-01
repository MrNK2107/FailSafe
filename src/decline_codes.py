"""
Real Razorpay card decline codes (razorpay.com/docs/errors/payments/cards/)
and the recovery policy we apply to each one.

The actual policy data lives in config/decline_policy.json, not here - a
merchant changes how a decline code is handled by editing that JSON file
directly, with no Python change and no redeploy. This module only loads
it, validates every entry against the two enums below, and exposes the
same DECLINE_CODES dict / get_decline_code() interface every caller
(agent.py, gate.py, generate_data.py) already depends on, so the swap from
a hardcoded dict to an external file is invisible to everything else.

This table is the deterministic ground truth. The agent's LLM proposes an
action per subscription, but the gate (see gate.py) checks every proposal
against ALLOWED_ACTIONS here and overrides anything out of policy. The LLM
never gets the final word on a money action.

RecoveryAction values:
  IMMEDIATE_RETRY      - transient failure, safe to retry right away
  DELAYED_RETRY        - customer-side issue likely to clear with time
                          (e.g. payday), retry after a cooldown
  PAYMENT_LINK_NUDGE   - can't safely auto-retry; send the customer a
                          payment link to fix it themselves
  NO_ACTION_FRAUD      - risk-flagged; never retry, flag to merchant only
  NO_ACTION_UNRECOVERABLE - customer-cancelled or blocked; nothing to do
"""

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

POLICY_PATH = Path(__file__).parent.parent / "config" / "decline_policy.json"


class RecoveryAction(str, Enum):
    IMMEDIATE_RETRY = "immediate_retry"
    DELAYED_RETRY = "delayed_retry"
    PAYMENT_LINK_NUDGE = "payment_link_nudge"
    NO_ACTION_FRAUD = "no_action_fraud"
    NO_ACTION_UNRECOVERABLE = "no_action_unrecoverable"


class DeclineSource(str, Enum):
    CUSTOMER = "customer"
    BANK = "bank"
    GATEWAY = "gateway"
    NETWORK = "network"


@dataclass(frozen=True)
class DeclineCode:
    code: str
    description: str
    source: DeclineSource
    allowed_action: RecoveryAction
    # Rough probability a nudge/retry actually succeeds, used ONLY by the
    # synthetic customer-response simulator in generate_data.py. This is a
    # labeled assumption, not a measured real-world rate - see BUILD_LOG.md §6.2.
    simulated_success_rate: float


def _load_decline_codes(path: Path) -> dict[str, DeclineCode]:
    """
    Loads and validates config/decline_policy.json. Fails loudly and
    specifically (which code, which field, why) on a bad edit - a merchant
    typo in `allowed_action` or `source` must never silently fall through
    to the gate with an invalid policy, since the gate trusts this table
    as ground truth.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    codes = {}
    for code, entry in raw.items():
        if code.startswith("_"):
            continue  # e.g. "_comment" - not a decline code
        try:
            source = DeclineSource(entry["source"])
        except ValueError:
            raise ValueError(
                f"{path.name}: {code!r} has invalid source {entry['source']!r}; "
                f"must be one of {[s.value for s in DeclineSource]}"
            )
        try:
            allowed_action = RecoveryAction(entry["allowed_action"])
        except ValueError:
            raise ValueError(
                f"{path.name}: {code!r} has invalid allowed_action {entry['allowed_action']!r}; "
                f"must be one of {[a.value for a in RecoveryAction]}"
            )
        rate = entry["simulated_success_rate"]
        if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not (0.0 <= rate <= 1.0):
            raise ValueError(
                f"{path.name}: {code!r} has invalid simulated_success_rate {rate!r}; "
                f"must be a number between 0.0 and 1.0"
            )

        codes[code] = DeclineCode(
            code=code,
            description=entry["description"],
            source=source,
            allowed_action=allowed_action,
            simulated_success_rate=float(rate),
        )
    return codes


DECLINE_CODES: dict[str, DeclineCode] = _load_decline_codes(POLICY_PATH)


def get_decline_code(code: str) -> DeclineCode:
    if code not in DECLINE_CODES:
        raise KeyError(f"Unknown decline code: {code!r}")
    return DECLINE_CODES[code]
