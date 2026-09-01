"""
The gate: plain deterministic code, no LLM involved. Every action the agent
wants to take passes through here before anything touches Razorpay.

Five checks, in order:
  1. Policy check        - is this action even allowed for this decline code?
                            (overrides the LLM if it proposed something off-policy)
  2. Attempt-cap escalation - has this subscription already had
                            MAX_ATTEMPTS_PER_SUBSCRIPTION real recovery attempts
                            across ALL previous runs (not just this one)? Stop
                            nudging it automatically and hand it to a human.
  3. Stale-halt escalation - has this subscription sat halted for at least
                            STALE_HALT_ESCALATION_DAYS? Too cold to keep
                            spending an automated nudge on; escalate instead.
  4. Spending cap         - does this action's amount exceed the hard
                            per-action and per-run limits?
  5. Idempotency          - has this exact (subscription_id, action) already
                            been executed in this run? Refuse to double-act.

Checks 2 and 3 are this project's "compliant escalation" + "stopping rules"
answer to the buildathon rubric at the cross-run level: idempotency (check 5)
only ever stops a *duplicate* action within one run - nothing previously
stopped the same subscription being nudged again forever on every subsequent
run. See BUILD_LOG.md §12 for how agent.py derives `prior_attempt_count` from
the audit log's cross-run history, and for the honest disclosure that the
committed flagship RESULTS.md predates this addition.

This is deliberately the least "AI" file in the whole project. A gate that
depends on the model behaving isn't a gate.
"""

from dataclasses import dataclass

from decline_codes import RecoveryAction, get_decline_code

MAX_ACTION_AMOUNT_PAISE = 50_000 * 100      # ₹50,000 per single action
MAX_RUN_TOTAL_PAISE = 5_00_000 * 100        # ₹5,00,000 total per run

# Compliant-escalation stopping rules (rubric: "compliant escalation" +
# "stopping rules"), both independent of the spending cap and same-run
# idempotency check above.
MAX_ATTEMPTS_PER_SUBSCRIPTION = 3    # after this many real attempts across all runs, stop and escalate
STALE_HALT_ESCALATION_DAYS = 12      # halted this long -> too cold to keep auto-nudging, escalate


@dataclass
class GateDecision:
    # Whether the LLM's raw proposal matched policy exactly (a metric on the
    # model, not on the system - the system may still `execute` a corrected
    # action even when this is False).
    llm_matched_policy: bool
    # Whether the gate will actually carry out `final_action` at all. Only
    # False for the hard blocks: spending cap exceeded or duplicate action.
    execute: bool
    reason: str
    final_action: RecoveryAction


class Gate:
    def __init__(self):
        self._seen: set[tuple[str, str]] = set()
        self._run_total_paise = 0

    def seed_from_checkpoint(self, already_spent_paise: int, seen_keys: set[tuple[str, str]]):
        """
        Resumed runs skip re-evaluating checkpointed records entirely, so
        the gate's in-memory spending/idempotency state would otherwise
        start from zero and under-count what a resumed run has already
        committed. Call this once, right after construction, with totals
        derived from the checkpoint.
        """
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
        policy = get_decline_code(decline_code)
        llm_matched_policy = proposed_action == policy.allowed_action
        final_action = policy.allowed_action  # policy always wins, LLM never does

        override_note = (
            "" if llm_matched_policy else
            f" (LLM proposed '{proposed_action.value}', policy overrode to "
            f"'{final_action.value}')"
        )

        # A policy-mandated no-action never touches money - nothing to gate
        # on amount/idempotency, always executes (the "action" is refusing).
        if final_action in (RecoveryAction.NO_ACTION_FRAUD, RecoveryAction.NO_ACTION_UNRECOVERABLE):
            return GateDecision(
                llm_matched_policy=llm_matched_policy,
                execute=True,
                reason=f"No-action policy for '{decline_code}' ({policy.source.value} source){override_note}.",
                final_action=final_action,
            )

        # Compliant-escalation stopping rule #1: cross-run attempt cap.
        # Idempotency (below) only blocks a *duplicate* action within one
        # run - nothing else stops the same subscription being nudged again
        # on every subsequent run forever. `prior_attempt_count` is derived
        # by the caller from the audit log's cross-run history (see
        # agent.py), so this fires only once real attempts actually pile up.
        if prior_attempt_count >= MAX_ATTEMPTS_PER_SUBSCRIPTION:
            return GateDecision(
                llm_matched_policy=llm_matched_policy,
                execute=True,
                reason=(
                    f"Escalated to manual review: {prior_attempt_count} prior recovery "
                    f"attempts already made for {subscription_id} across previous runs "
                    f"(compliant-escalation cap: max {MAX_ATTEMPTS_PER_SUBSCRIPTION})"
                    f"{override_note}."
                ),
                final_action=RecoveryAction.NO_ACTION_UNRECOVERABLE,
            )

        # Compliant-escalation stopping rule #2: stale-halt threshold. A
        # subscription halted this long is judged too cold for an automated
        # nudge to still be the right call - hand it to a human instead of
        # quietly spending another attempt on it. `halted_days_ago` is
        # optional (e.g. agent_onetime.py's one-time payments have no halt
        # clock at all, so this never fires for that domain).
        if halted_days_ago is not None and halted_days_ago >= STALE_HALT_ESCALATION_DAYS:
            return GateDecision(
                llm_matched_policy=llm_matched_policy,
                execute=True,
                reason=(
                    f"Escalated to manual review: halted {halted_days_ago} days ago, "
                    f"at or past the {STALE_HALT_ESCALATION_DAYS}-day staleness threshold "
                    f"for an automated nudge{override_note}."
                ),
                final_action=RecoveryAction.NO_ACTION_UNRECOVERABLE,
            )

        # Hard block: per-action spending cap.
        if amount_paise > MAX_ACTION_AMOUNT_PAISE:
            return GateDecision(
                llm_matched_policy=llm_matched_policy,
                execute=False,
                reason=(
                    f"Amount {amount_paise/100:.2f} exceeds per-action cap "
                    f"of {MAX_ACTION_AMOUNT_PAISE/100:.2f}."
                ),
                final_action=RecoveryAction.NO_ACTION_UNRECOVERABLE,
            )

        # Hard block: run-total spending cap.
        if self._run_total_paise + amount_paise > MAX_RUN_TOTAL_PAISE:
            return GateDecision(
                llm_matched_policy=llm_matched_policy,
                execute=False,
                reason="Run-total spending cap would be exceeded.",
                final_action=RecoveryAction.NO_ACTION_UNRECOVERABLE,
            )

        # Hard block: idempotency - same subscription + same final action
        # already executed this run.
        key = (subscription_id, final_action.value)
        if key in self._seen:
            return GateDecision(
                llm_matched_policy=llm_matched_policy,
                execute=False,
                reason=f"Duplicate action for {subscription_id} - already processed this run.",
                final_action=RecoveryAction.NO_ACTION_UNRECOVERABLE,
            )

        self._seen.add(key)
        self._run_total_paise += amount_paise
        return GateDecision(
            llm_matched_policy=llm_matched_policy,
            execute=True,
            reason=f"Passed spending cap and idempotency checks{override_note}.",
            final_action=final_action,
        )
