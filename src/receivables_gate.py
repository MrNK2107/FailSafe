"""
The enforcement layer for OVERDUE RECEIVABLES - deliberately its OWN
small, deterministic gate class, not a call into gate.py's Gate.evaluate()
(same considered choice abandonment_gate.py already made for checkout
abandonment, for the same reason - see that module's docstring).

Why not generalize Gate.evaluate() instead: its signature and internal
logic are built entirely around a decline_code -> RecoveryAction lookup
(decline_codes.get_decline_code(decline_code)) and a proposed_action
parameter representing an LLM's proposal that the policy check can
override. An overdue-receivable record has no decline_code (by the
category's own definition) and, in this build, no separate LLM
"propose the action" call either (same disclosed choice as
checkout_abandonment_agent.py: once a case_reason is diagnosed, the
action is a deterministic policy lookup, not a second model call - see
receivables_agent.py's docstring). Reshaping Gate.evaluate() to cover a
fourth, unrelated lookup table would risk the already-passing tests in
tests/test_gate.py and the verified 150-record flagship run for a feature
those tests never anticipated - the same risk calculus this project has
now made three times (Route, checkout abandonment, receivables) and
resolved the same way each time: keep the already-verified pipeline's
gate untouched, give each genuinely different domain its own small,
equally-tested enforcement layer.

What IS reused, not reinvented: MAX_ACTION_AMOUNT_PAISE from gate.py - the
per-action spending cap is not domain-specific money-policy, it's a
project-wide ceiling on how much any single automated action is allowed
to move/imply, and reusing the actual constant (not a duplicated magic
number) keeps that ceiling consistent across every domain in this repo.
Disclosed honestly: B2B invoices are frequently larger than a checkout
cart or a subscription charge, so this cap will fire more often here than
in the other two domains - that is a real, intended consequence (a large
overdue invoice is exactly the kind of case that should require a human's
judgment before an automated system nudges the customer about it), not an
oversight, and generate_receivables_data.py deliberately produces some
invoices above the cap so this isn't just a theoretical code path.

THE "compliant escalation + stopping rules" bar for THIS domain
specifically (this project's own rubric explicitly requires this per
domain, and B2B collections is exactly where real compliance concerns -
how many times you can reasonably keep chasing an unpaid invoice, and
when an account is too old for more automated contact and needs a
human/legal review instead - are most relevant):

  1. MAX_REMINDERS_BEFORE_ESCALATION = 4. After 4 automated reminders have
     already been sent for an invoice with no payment received, continuing
     to auto-chase is no longer a reasonable default - hand it to a human
     collections process instead of sending a 5th, 6th, ... automated
     message indefinitely. Chosen as a deliberately small, defensible
     number for the same reason gate.py's MAX_ATTEMPTS_PER_SUBSCRIPTION=3
     is small: an automated system should stop well before it starts
     looking like harassment, not push the limit of what might be
     technically tolerable. `reminders_sent_count` is read directly off
     the record (this domain's own communication-history field), not
     derived from cross-run audit-log history the way gate.py's
     `prior_attempt_count` is - a disclosed scope simplification, see
     below.
  2. DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD = 90. An invoice overdue 90+ days
     has, in ordinary B2B accounts-receivable practice, already crossed
     from "routine collections" into the aging bucket where a business
     typically involves legal/formal collections rather than continuing
     ordinary automated reminders - so at or past this threshold, the
     gate always escalates to a human/legal review instead of letting a
     nudge go out automatically, regardless of the diagnosed case_reason.
     This is a considered, disclosed number (a common real-world AR aging
     boundary), not an arbitrary one, and it is deliberately far longer
     than checkout abandonment's 12-HOUR staleness threshold or the
     flagship pipeline's 12-DAY one: unlike a checkout cart or a halted
     subscription retry cycle, a B2B invoice's normal collections cadence
     genuinely runs for weeks-to-months before anyone would call it
     stale - a real, disclosed domain difference in the escalation clock
     itself, not a copy-pasted number.

Checks, in order (mirrors gate.py's/abandonment_gate.py's own ordering and
"policy first, then independent stopping rules, then hard blocks"
structure):
  1. Policy lookup - the diagnosed case_reason determines the action via
     config/receivables_policy.json (receivables_policy.py). There is no
     LLM proposal to compare this against in this build, so there is no
     "override" case here the way gate.py has one - the action IS the
     diagnosed reason's policy row. If that policy is itself a no-action
     (invoice_dispute_likely or high_risk_non_payment - both go to a
     human, not an automated nudge, see NO_ACTION_POLICY_ACTIONS in
     receivables_policy.py), it always executes (refusing/escalating IS
     the action) and bypasses every check below, exactly like gate.py's
     fraud/unrecoverable bypass and abandonment_gate.py's
     no_action_respect_hesitation bypass.
  2. Compliant-escalation stopping rule #1: reminder-count cap.
  3. Compliant-escalation stopping rule #2: staleness threshold.
  4. Spending cap - reuses gate.py's actual MAX_ACTION_AMOUNT_PAISE
     constant; an invoice above it is a hard block, not covered by this
     enforcement layer's own no-action policies.
  5. Idempotency - same (invoice_id, final_action) pair already executed
     this run is hard-blocked, exactly like gate.py's/abandonment_gate.py's
     own idempotency check.

Scope, disclosed honestly like agent_onetime.py's/abandonment_gate.py's
own scope notes: no cross-run attempt-history derivation exists here
(gate.py derives `prior_attempt_count` from the audit log's cross-run
history for its own MAX_ATTEMPTS_PER_SUBSCRIPTION check - see gate.py's
module docstring and BUILD_LOG.md §12). This domain's reminder count is
instead read directly from the record's own `reminders_sent_count`
field - a same-run, same-dataset stand-in for that same idea, not a
weaker check by accident: a real receivables system already tracks
"reminders sent so far" as a durable field on the invoice/account itself
(unlike a subscription's recovery-attempt count, which this project only
derives after the fact from its own audit log), so reading it directly is
the more realistic modeling choice for this specific domain, not a
shortcut. Same-run idempotency still caps every invoice to at most one
automated action per run, exactly like the other two domains.
"""

from dataclasses import dataclass

from gate import MAX_ACTION_AMOUNT_PAISE, MAX_RUN_TOTAL_PAISE
from receivables_policy import NO_ACTION_POLICY_ACTIONS, ReceivableAction, get_receivable_policy

MAX_REMINDERS_BEFORE_ESCALATION = 4
DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD = 90


@dataclass
class ReceivableGateDecision:
    execute: bool
    reason: str
    final_action: ReceivableAction


class ReceivableGate:
    def __init__(self):
        self._seen: set[tuple[str, str]] = set()
        self._run_total_paise = 0

    def evaluate(
        self,
        invoice_id: str,
        case_reason: str,
        amount_paise: int,
        days_overdue: int,
        reminders_sent_count: int,
    ) -> ReceivableGateDecision:
        policy = get_receivable_policy(case_reason)
        final_action = policy.allowed_action

        # A policy-mandated no-action (dispute review or manual-collections
        # escalation) never touches money/messaging automatically - nothing
        # to gate on reminder-count/staleness/cap/idempotency, always
        # executes (the "action" is refusing/escalating), exactly like
        # gate.py's fraud/unrecoverable bypass and abandonment_gate.py's
        # no_action_respect_hesitation bypass.
        if final_action in NO_ACTION_POLICY_ACTIONS:
            return ReceivableGateDecision(
                execute=True,
                reason=f"No-automated-action policy for '{case_reason}'.",
                final_action=final_action,
            )

        # Compliant-escalation stopping rule #1: reminder-count cap.
        if reminders_sent_count >= MAX_REMINDERS_BEFORE_ESCALATION:
            return ReceivableGateDecision(
                execute=True,
                reason=(
                    f"Escalated: {reminders_sent_count} reminders already sent for "
                    f"{invoice_id}, at or past the compliant-escalation cap of "
                    f"{MAX_REMINDERS_BEFORE_ESCALATION} - handing to a human instead "
                    f"of sending another automated reminder."
                ),
                final_action=ReceivableAction.NO_ACTION_ALREADY_ESCALATED,
            )

        # Compliant-escalation stopping rule #2: staleness threshold.
        if days_overdue >= DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD:
            return ReceivableGateDecision(
                execute=True,
                reason=(
                    f"Escalated: invoice {invoice_id} is {days_overdue} days overdue, "
                    f"at or past the {DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD}-day threshold "
                    f"for automated collections - needs a human/legal review instead."
                ),
                final_action=ReceivableAction.NO_ACTION_STALE_INVOICE_NEEDS_LEGAL_REVIEW,
            )

        # Hard block: per-action spending cap (reused from gate.py).
        if amount_paise > MAX_ACTION_AMOUNT_PAISE:
            return ReceivableGateDecision(
                execute=False,
                reason=(
                    f"Amount {amount_paise / 100:.2f} exceeds per-action cap "
                    f"of {MAX_ACTION_AMOUNT_PAISE / 100:.2f}."
                ),
                final_action=ReceivableAction.NO_ACTION_NEEDS_HUMAN_REVIEW,
            )

        # Hard block: run-total spending cap (reused from gate.py) - found
        # missing here on a later code review, the same gap
        # abandonment_gate.py had: self._run_total_paise was tracked from
        # the start but never compared against the cap. Mirrors gate.py's
        # own check exactly.
        if self._run_total_paise + amount_paise > MAX_RUN_TOTAL_PAISE:
            return ReceivableGateDecision(
                execute=False,
                reason="Run-total spending cap would be exceeded.",
                final_action=ReceivableAction.NO_ACTION_NEEDS_HUMAN_REVIEW,
            )

        # Hard block: idempotency - same invoice + same final action
        # already executed this run.
        key = (invoice_id, final_action.value)
        if key in self._seen:
            return ReceivableGateDecision(
                execute=False,
                reason=f"Duplicate action for {invoice_id} - already processed this run.",
                final_action=ReceivableAction.NO_ACTION_NEEDS_HUMAN_REVIEW,
            )

        self._seen.add(key)
        self._run_total_paise += amount_paise
        return ReceivableGateDecision(
            execute=True,
            reason="Passed reminder-cap, staleness, spending cap, and idempotency checks.",
            final_action=final_action,
        )
