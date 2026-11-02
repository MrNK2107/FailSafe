from collections.abc import Mapping
from dataclasses import dataclass

from failsafe.base_gate import MAX_ACTION_AMOUNT_PAISE as _MAX_ACTION
from failsafe.base_gate import MAX_RUN_TOTAL_PAISE as _MAX_RUN
from failsafe.base_gate import SpendingCapMixin
from failsafe.decline_codes import DeclineCode, RecoveryAction, get_decline_code

MAX_ACTION_AMOUNT_PAISE = _MAX_ACTION
MAX_RUN_TOTAL_PAISE = _MAX_RUN

MAX_ATTEMPTS_PER_SUBSCRIPTION = 3
STALE_HALT_ESCALATION_DAYS = 12


@dataclass
class GateDecision:
    llm_matched_policy: bool
    execute: bool
    reason: str
    final_action: RecoveryAction


class Gate(SpendingCapMixin):
    def __init__(self, policy: Mapping[str, DeclineCode] | None = None):
        # policy=None uses the live config/decline_policy.json table. A replay
        # (src/failsafe/replay.py) passes a candidate table to ask "what would this
        # policy have decided?" without touching the global one.
        self._policy = policy
        self._seen: set[tuple[str, str]] = set()
        self._run_total_paise = 0

    def _lookup(self, decline_code: str) -> DeclineCode:
        if self._policy is None:
            return get_decline_code(decline_code)
        if decline_code not in self._policy:
            raise KeyError(f"Unknown decline code: {decline_code!r}")
        return self._policy[decline_code]

    def seed_from_checkpoint(self, already_spent_paise: int, seen_keys: set[tuple[str, str]]):
        self._run_total_paise = already_spent_paise
        self._seen |= seen_keys

    def evaluate(
        self,
        subscription_id: str,
        decline_code: str,
        proposed_action: RecoveryAction,
        amount_paise: int,
        prior_attempt_count: int = 0,
        halted_days_ago: int | None = None,
    ) -> GateDecision:
        policy = self._lookup(decline_code)
        llm_matched_policy = proposed_action == policy.allowed_action
        final_action = policy.allowed_action
        override_note = (
            ""
            if llm_matched_policy
            else f" (LLM proposed '{proposed_action.value}', policy overrode to '{final_action.value}')"
        )

        if final_action in (RecoveryAction.NO_ACTION_FRAUD, RecoveryAction.NO_ACTION_UNRECOVERABLE):
            return GateDecision(
                llm_matched_policy=llm_matched_policy,
                execute=True,
                reason=f"No-action policy for '{decline_code}' ({policy.source.value} source){override_note}.",
                final_action=final_action,
            )

        if prior_attempt_count >= MAX_ATTEMPTS_PER_SUBSCRIPTION:
            return GateDecision(
                llm_matched_policy=llm_matched_policy,
                execute=True,
                reason=f"Escalated to manual review: {prior_attempt_count} prior recovery attempts already made for {subscription_id} across previous runs (compliant-escalation cap: max {MAX_ATTEMPTS_PER_SUBSCRIPTION}){override_note}.",
                final_action=RecoveryAction.NO_ACTION_UNRECOVERABLE,
            )

        if halted_days_ago is not None and halted_days_ago >= STALE_HALT_ESCALATION_DAYS:
            return GateDecision(
                llm_matched_policy=llm_matched_policy,
                execute=True,
                reason=f"Escalated to manual review: halted {halted_days_ago} days ago, at or past the {STALE_HALT_ESCALATION_DAYS}-day staleness threshold for an automated nudge{override_note}.",
                final_action=RecoveryAction.NO_ACTION_UNRECOVERABLE,
            )

        ok, reason = self._enforce_cap(amount_paise)
        if not ok:
            return GateDecision(
                llm_matched_policy=llm_matched_policy,
                execute=False,
                reason=reason,
                final_action=RecoveryAction.NO_ACTION_UNRECOVERABLE,
            )

        key = (subscription_id, final_action.value)
        if self._check_idempotency(key):
            return GateDecision(
                llm_matched_policy=llm_matched_policy,
                execute=False,
                reason=f"Duplicate action for {subscription_id} - already processed this run.",
                final_action=RecoveryAction.NO_ACTION_UNRECOVERABLE,
            )

        self._record(key, amount_paise)
        return GateDecision(
            llm_matched_policy=llm_matched_policy,
            execute=True,
            reason=f"Passed spending cap and idempotency checks{override_note}.",
            final_action=final_action,
        )


# The order Gate.evaluate() applies its checks in. It stops at the first one
# that decides the record; later checks are never reached. Used by the
# decision trace (src/failsafe/decision_trace.py) and replay (src/failsafe/replay.py) to show
# *which* safety check decided a recorded decision.
GATE_CHECKS: tuple[tuple[str, str], ...] = (
    ("policy", "Policy table lookup"),
    ("no_action_policy", "No-action policy (fraud / unrecoverable)"),
    ("attempt_cap", f"Attempt cap (max {MAX_ATTEMPTS_PER_SUBSCRIPTION} across runs)"),
    ("staleness", f"Staleness ({STALE_HALT_ESCALATION_DAYS}+ days halted)"),
    ("spend_cap", "Spending caps (per action / per run)"),
    ("idempotency", "Idempotency (once per record per run)"),
)


def deciding_check(reason: str) -> str:
    """Name of the GATE_CHECKS entry that produced `reason` (a GateDecision.reason
    from Gate.evaluate), or "passed" when every check passed. Matches the
    fixed reason prefixes above, so it also works on audit logs written before
    this function existed."""
    if reason.startswith("No-action policy"):
        return "no_action_policy"
    if "prior recovery attempts" in reason:
        return "attempt_cap"
    if reason.startswith("Escalated to manual review: halted"):
        return "staleness"
    if "per-action cap" in reason or reason.startswith("Run-total spending cap"):
        return "spend_cap"
    if reason.startswith("Duplicate action"):
        return "idempotency"
    return "passed"
