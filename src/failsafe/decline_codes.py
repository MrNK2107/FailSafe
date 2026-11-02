import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from failsafe.paths import CONFIG_DIR

POLICY_PATH = CONFIG_DIR / "decline_policy.json"


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
    simulated_success_rate: float


def _load_decline_codes(path: Path) -> dict[str, DeclineCode]:
    from failsafe.policy_loader import load_policy

    validated = load_policy(path, None, RecoveryAction, extra_validators={"source": DeclineSource})
    codes = {}
    for code, entry in validated.items():
        codes[code] = DeclineCode(
            code=code,
            description=entry["description"],
            source=DeclineSource(entry["source"]),
            allowed_action=RecoveryAction(entry["allowed_action"]),
            simulated_success_rate=float(entry["simulated_success_rate"]),
        )
    return codes


DECLINE_CODES: dict[str, DeclineCode] = _load_decline_codes(POLICY_PATH)


def get_decline_code(code: str) -> DeclineCode:
    if code not in DECLINE_CODES:
        raise KeyError(f"Unknown decline code: {code!r}")
    return DECLINE_CODES[code]


def load_decline_codes(path: str | Path) -> dict[str, DeclineCode]:
    """Load and validate a decline policy file (same rules as the live one).
    Used by src/failsafe/replay.py to evaluate a candidate policy before it ships."""
    return _load_decline_codes(Path(path))
