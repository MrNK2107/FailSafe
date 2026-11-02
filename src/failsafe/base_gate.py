from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

Reason = TypeVar("Reason")
Action = TypeVar("Action")
# Protocol type params must be covariant (they only appear in return position).
_Reason_co = TypeVar("_Reason_co", covariant=True)
_Action_co = TypeVar("_Action_co", covariant=True)

MAX_ACTION_AMOUNT_PAISE = 50_000 * 100
MAX_RUN_TOTAL_PAISE = 5_00_000 * 100


@dataclass
class GateDecision(Generic[Action]):
    execute: bool
    reason: str
    final_action: Action
    llm_matched_policy: bool | None = None


class BaseGate(Protocol[_Reason_co, _Action_co]):
    def evaluate(self, *args, **kwargs) -> GateDecision: ...


class SpendingCapMixin:
    _run_total_paise: int
    _seen: set[tuple[str, str]]

    def _enforce_cap(self, amount_paise: int) -> tuple[bool, str]:
        if amount_paise > MAX_ACTION_AMOUNT_PAISE:
            return (
                False,
                f"Amount {amount_paise / 100:.2f} exceeds per-action cap of {MAX_ACTION_AMOUNT_PAISE / 100:.2f}.",
            )
        if self._run_total_paise + amount_paise > MAX_RUN_TOTAL_PAISE:
            return False, "Run-total spending cap would be exceeded."
        return True, ""

    def _check_idempotency(self, key: tuple[str, str]) -> bool:
        return key in getattr(self, "_seen", set())

    def _record(self, key: tuple[str, str], amount_paise: int):
        self._seen.add(key)
        self._run_total_paise += amount_paise
