"""
The enforcement layer for CHECKOUT ABANDONMENT - deliberately its OWN
small, deterministic gate class, not a call into gate.py's Gate.evaluate().
This is a considered choice, explained honestly here rather than asserted,
mirroring how route_demo.py's own docstring explains why it doesn't call
Gate.evaluate() either.

Why not generalize Gate.evaluate() instead: its signature and internal
logic are built entirely around a decline_code -> RecoveryAction lookup
(decline_codes.get_decline_code(decline_code)) and a proposed_action
parameter representing an LLM's proposal that the policy check can
override. A checkout-abandonment record has no decline_code (by the
category's own definition - no payment was ever attempted or declined,
see checkout_abandonment_policy.py's docstring) and, in this build, no
separate LLM "propose the action" call either (see
checkout_abandonment_agent.py's docstring for why action-selection here
is a deterministic policy lookup, not a second model call) - there is
nothing for a generalized `evaluate()` to compare a proposal against.
Reshaping Gate.evaluate()'s signature to cover both shapes would either
(a) bolt a third, unrelated lookup table onto a class that 8+ already-
passing tests (tests/test_gate.py) and the verified 150-record flagship
run depend on, risking a regression in already-proven code for a feature
those tests never anticipated, or (b) grow a parallel code path inside
the same class that's harder to audit than just... a second small class.
Given this project's own precedent (Route: a new scenario, its own small
check, reusing only the gate's spending-cap *value*, not its class) the
same choice is made again here, for the same reason: keep the
already-verified pipeline's gate untouched, and give this genuinely
different domain its own small, equally-tested enforcement layer.

What IS reused, not reinvented: MAX_ACTION_AMOUNT_PAISE from gate.py - the
per-action spending cap is not domain-specific money-policy, it's a
project-wide ceiling on how much any single automated action is allowed
to move/imply, and reusing the actual constant (not a duplicated magic
number) keeps that ceiling consistent across every domain in this repo.

Checks, in order (mirrors gate.py's own ordering and "policy first, then
independent stopping rules, then hard blocks" structure):
  1. Policy lookup - the diagnosed abandonment_reason determines the
     action via config/abandonment_policy.json (checkout_abandonment_policy.py).
     There is no LLM proposal to compare this against in this build (see
     module docstring above), so there is no "override" case here the way
     gate.py has one - the action IS the diagnosed reason's policy row.
     If that policy is itself a no-action (trust_or_security_concern), it
     always executes (refusing IS the action) and bypasses every check
     below, exactly like gate.py's fraud/unrecoverable bypass.
  2. Stale-abandonment stopping rule - too much time has passed for a
     nudge to still make sense. Checkout carts go stale on the order of
     HOURS, not the 12 DAYS gate.py uses for halted subscriptions - a
     genuine, disclosed domain difference: nobody expects a payment link
     from an hours-old cart to still feel timely a day later, but a
     subscription's automated retry cadence is measured in days already.
  3. Low-value stopping rule, new to this domain (no equivalent in
     gate.py, which only ever caps a MAXIMUM): a cart below
     MIN_CART_VALUE_FOR_ACTION_PAISE isn't worth spending a messaging
     touch (and the customer-annoyance risk) on.
  4. Spending cap - reuses gate.py's actual MAX_ACTION_AMOUNT_PAISE
     constant; a cart above it is a hard block, not covered by this
     enforcement layer's own no-action policies.
  5. Idempotency - same (cart_id, final_action) pair already executed
     this run is hard-blocked, exactly like gate.py's own idempotency
     check.

Scope, disclosed honestly like agent_onetime.py's own scope note: no
cross-run attempt-cap escalation exists here (gate.py's
MAX_ATTEMPTS_PER_SUBSCRIPTION equivalent) - this is a standalone,
same-run-only demonstration (no checkpoint/resume, no audit-log history
lookup), same considered scope limit agent_onetime.py already accepted
for the same reason: not worth building cross-run history-derivation
twice for a stretch-scope demo. Same-run idempotency already caps every
cart to at most one automated action per run, which is this demo's
concrete realization of "per-cart attempt cap."
"""

from dataclasses import dataclass

from checkout_abandonment_policy import AbandonmentAction, get_abandonment_policy
from gate import MAX_ACTION_AMOUNT_PAISE, MAX_RUN_TOTAL_PAISE

# New to this domain: a MINIMUM value floor (gate.py only ever caps a
# maximum). Below this, an automated nudge isn't worth sending.
MIN_CART_VALUE_FOR_ACTION_PAISE = 149 * 100  # Rs 149

# Checkout carts go stale far faster than a halted subscription - hours,
# not gate.py's 12 days for STALE_HALT_ESCALATION_DAYS.
STALE_ABANDONMENT_MINUTES_THRESHOLD = 720  # 12 hours


@dataclass
class AbandonmentGateDecision:
    execute: bool
    reason: str
    final_action: AbandonmentAction


class AbandonmentGate:
    def __init__(self):
        self._seen: set[tuple[str, str]] = set()
        self._run_total_paise = 0

    def evaluate(
        self,
        cart_id: str,
        abandonment_reason: str,
        amount_paise: int,
        minutes_since_abandonment: int,
    ) -> AbandonmentGateDecision:
        policy = get_abandonment_policy(abandonment_reason)
        final_action = policy.allowed_action

        # A policy-mandated no-action never touches money/messaging -
        # nothing to gate on value/staleness/cap/idempotency, always
        # executes (the "action" is refusing), exactly like gate.py's
        # fraud/unrecoverable bypass.
        if final_action == AbandonmentAction.NO_ACTION_RESPECT_HESITATION:
            return AbandonmentGateDecision(
                execute=True,
                reason=f"No-action policy for '{abandonment_reason}'.",
                final_action=final_action,
            )

        # Stopping rule: stale abandonment.
        if minutes_since_abandonment >= STALE_ABANDONMENT_MINUTES_THRESHOLD:
            return AbandonmentGateDecision(
                execute=True,
                reason=(
                    f"Escalated/skipped: abandoned {minutes_since_abandonment} minutes ago, "
                    f"at or past the {STALE_ABANDONMENT_MINUTES_THRESHOLD}-minute staleness "
                    f"threshold for an automated nudge."
                ),
                final_action=AbandonmentAction.NO_ACTION_STALE_ABANDONMENT,
            )

        # Stopping rule: cart value too low to be worth an automated touch.
        if amount_paise < MIN_CART_VALUE_FOR_ACTION_PAISE:
            return AbandonmentGateDecision(
                execute=True,
                reason=(
                    f"Cart amount {amount_paise / 100:.2f} is below the "
                    f"{MIN_CART_VALUE_FOR_ACTION_PAISE / 100:.2f} minimum for an automated nudge."
                ),
                final_action=AbandonmentAction.NO_ACTION_LOW_VALUE,
            )

        # Hard block: per-action spending cap (reused from gate.py).
        if amount_paise > MAX_ACTION_AMOUNT_PAISE:
            return AbandonmentGateDecision(
                execute=False,
                reason=(
                    f"Amount {amount_paise / 100:.2f} exceeds per-action cap "
                    f"of {MAX_ACTION_AMOUNT_PAISE / 100:.2f}."
                ),
                final_action=AbandonmentAction.NO_ACTION_NEEDS_HUMAN_REVIEW,
            )

        # Hard block: run-total spending cap (reused from gate.py) - found
        # missing here on a later code review (this class tracked
        # self._run_total_paise from the start but never compared it
        # against the cap, so the run-total stopping rule was silently
        # absent for this domain despite this module's own docstring
        # claiming it was reused). Mirrors gate.py's own check exactly.
        if self._run_total_paise + amount_paise > MAX_RUN_TOTAL_PAISE:
            return AbandonmentGateDecision(
                execute=False,
                reason="Run-total spending cap would be exceeded.",
                final_action=AbandonmentAction.NO_ACTION_NEEDS_HUMAN_REVIEW,
            )

        # Hard block: idempotency - same cart + same final action already
        # executed this run.
        key = (cart_id, final_action.value)
        if key in self._seen:
            return AbandonmentGateDecision(
                execute=False,
                reason=f"Duplicate action for {cart_id} - already processed this run.",
                final_action=AbandonmentAction.NO_ACTION_NEEDS_HUMAN_REVIEW,
            )

        self._seen.add(key)
        self._run_total_paise += amount_paise
        return AbandonmentGateDecision(
            execute=True,
            reason="Passed value floor, staleness, spending cap, and idempotency checks.",
            final_action=final_action,
        )
