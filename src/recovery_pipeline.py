"""
Shared per-record recovery pipeline: diagnose -> propose -> gate -> execute -> audit.

Extracted from agent.py and agent_onetime.py, which had grown ~80%
duplicated (near-identical `process_one` bodies differing mainly in field
names and situation text) - a real code-quality gap found on re-auditing
the codebase against the buildathon rubric, see BUILD_LOG.md §12. This
module is the one place that logic now lives; both callers pass in only
what's genuinely domain-specific (the id field name, the situation
description, the item-label field for a payment-link's description).

Deliberately NOT here: checkpoint/resume and the CLI --inject-failure
plumbing. Those stay in agent.py, since agent_onetime.py's stretch-goal
pipeline was scoped without them on purpose (BUILD_LOG.md §13) - keeping
them out of this shared module means agent_onetime.py never has to know
they exist.

Root-cause diagnosis stage (`raw_signal_field`, see process_record below):
before this existed, decline_code was consumed everywhere as ground truth
handed in by generate_data.py - nothing in the pipeline ever inferred it.
See diagnose.py's docstring and PS_REQUIREMENTS_DEBATE.md Round 2. The
diagnosis stage is OPT-IN per call site via `raw_signal_field`: agent.py's
halted-subscription records now carry `raw_decline_message`
(generate_data.py) and pass `raw_signal_field="raw_decline_message"`, so
they get genuinely diagnosed. agent_onetime.py does not pass it, so its
records fall through to the pre-diagnosis behavior unchanged (ground-truth
decline_code used directly) - a real, disclosed scope limit, not an
oversight: see BUILD_LOG.md and README.md §6 for why the one-time-payment
pipeline was not also re-plumbed in this change. Existing unit tests that
construct minimal record dicts with no `raw_decline_message` field also
fall through this same path unchanged, which is why none of them needed
to be touched for this feature to land.

Revenue-at-risk DETECTION stage (`run_detection`, see process_record
below): before this existed, nothing in the pipeline ever decided WHICH
records deserve attention at all - agent.py unconditionally processes
every record in data/halted_subscriptions.json, a file whose name and
contents guarantee every record is already at-risk. See detect.py's
docstring and PS_REQUIREMENTS_DEBATE.md Round 2, finding 4. Detection is
OPT-IN per call site via `run_detection=True`, and runs BEFORE everything
else in this function, including the unknown-decline-code check and
diagnosis: a record classified "leave_alone" returns immediately, never
reaching diagnosis, the action proposal, or the gate - a real short-
circuit, not a logged-and-ignored label. Neither agent.py nor
agent_onetime.py opts in today (their datasets don't carry the detection
signal fields at all) - see detection_live_demo.py for the standalone
demonstration of this stage wired into the real pipeline end to end.
"""

from mcp import Client

from audit_log import AuditLogger
from decline_codes import DECLINE_CODES, RecoveryAction, get_decline_code
from detect import LEAVE_ALONE, NEEDS_ATTENTION, detect_at_risk
from diagnose import diagnose_decline_code
from gate import MAX_ATTEMPTS_PER_SUBSCRIPTION, Gate
from ollama_client import propose_action

LEFT_ALONE_BY_DETECTION = "left_alone_by_detection"

# Tools that represent a real recovery *attempt* on a record - used to
# derive prior_attempt_count (gate.py's cross-run attempt-cap stopping
# rule) from the audit log's history. flag_for_manual_review is
# deliberately excluded: escalating to a human isn't a retry attempt.
RECOVERY_ATTEMPT_TOOLS = {"create_payment_link", "create_retry_order"}


def count_prior_attempts(audit_events: list[dict], id_field: str, record_id: str) -> int:
    """
    How many real recovery-action tool calls already exist for this record
    across ALL previous runs. The audit log is append-only and never
    cleared between runs, so this reflects true cross-run history, not
    just this run's in-memory state - feeds gate.py's
    MAX_ATTEMPTS_PER_SUBSCRIPTION stopping rule. See BUILD_LOG.md §12.
    """
    return sum(
        1 for e in audit_events
        if e.get("event_type") == "mcp_tool_call"
        and e.get(id_field) == record_id
        and e.get("tool") in RECOVERY_ATTEMPT_TOOLS
    )


def extract_tool_text(call) -> str | None:
    """Defensively pull text out of an MCP CallToolResult - don't assume
    content[0] exists or is text-typed, even though every tool here always
    returns exactly that shape today."""
    if not call.content:
        return None
    first = call.content[0]
    return getattr(first, "text", None)


async def process_record(
    client: Client,
    gate: Gate,
    audit: AuditLogger,
    record: dict,
    *,
    id_field: str,
    action_to_tool: dict,
    item_label_field: str,
    situation: str,
    record_label: str,
    inject_failure: str | None = None,
    prior_attempt_count: int = 0,
    raw_signal_field: str | None = None,
    run_detection: bool = False,
) -> dict:
    """
    Run one record through the full detect -> diagnose -> propose -> gate
    -> execute -> audit sequence. Domain-agnostic: agent.py calls this with
    id_field="subscription_id" for halted subscriptions, agent_onetime.py
    with id_field="payment_id" for failed one-time payments - see each
    caller for its own `situation`/`record_label`/`item_label_field`.

    `run_detection`, when True, runs a real detection step (detect.py)
    BEFORE everything else in this function - before the unknown-decline-
    code check, before diagnosis, before the action proposal. It reads
    four fixed signal fields off `record`: `previous_retry_count`,
    `days_since_last_successful_charge`, `most_recent_gateway_response`,
    `subscription_status`. A "leave_alone" classification returns
    immediately: no diagnosis, no action proposal, no gate call, no MCP
    tool call at all - a real short-circuit, not a logged-and-ignored
    label. A "needs_recovery_attention" classification (or a failed
    detection call, which fails safe toward NOT silently dropping
    possible revenue) falls through into the rest of this function
    unchanged. `record.get("ground_truth_needs_attention")` is read ONLY
    to log `detection_matched_ground_truth` for honest accuracy
    measurement - exactly like `diagnosis_matched_ground_truth` and
    `llm_matched_policy` measure their own stages without feeding back
    into the decision. Defaults to False so every existing caller
    (agent.py, agent_onetime.py, every pre-existing test fixture) is
    completely unaffected.

    `raw_signal_field`, when given and present (non-empty) on `record`,
    names the field holding a raw, ambiguous bank/gateway decline message
    (e.g. "raw_decline_message"). When present, this function runs a real
    diagnosis step (diagnose.py) BEFORE the action proposal, and uses the
    DIAGNOSED decline_code - not record[id_field]'s ground-truth
    decline_code - for the action proposal's prompt, gate.evaluate(), and
    execution. Ground truth is still read, but ONLY to log
    `diagnosis_matched_ground_truth` for honest accuracy measurement,
    exactly like `llm_matched_policy` already measures action-proposal
    accuracy against policy - it never feeds back into the decision. When
    omitted (or the field is missing/falsy on this record), diagnosis is
    skipped entirely and record[id_field]'s decline_code is used directly,
    unchanged from this module's pre-diagnosis behavior.
    """
    record_id = record[id_field]
    id_kwargs = {id_field: record_id}

    # Populated only when run_detection=True and the call actually returned
    # a classification (not a failure) - threaded into every return dict
    # below (not just the early "leave_alone" one) so a caller can always
    # look at result["detection_classification"] uniformly, whichever path
    # a record took.
    detection_classification: str | None = None
    detection_matched_ground_truth: bool | None = None

    if run_detection:
        ground_truth_needs_attention = record.get("ground_truth_needs_attention")

        if inject_failure == "detection_parse_failure":
            detection = {
                "classification": None,
                "reasoning": "[injected for demo] simulated: model returned no usable detection tool call",
            }
        else:
            detection = detect_at_risk(
                previous_retry_count=record.get("previous_retry_count", 0),
                days_since_last_successful_charge=record.get("days_since_last_successful_charge", 0),
                most_recent_gateway_response=record.get("most_recent_gateway_response"),
                subscription_status=record.get("subscription_status", "unknown"),
            )
        classification = detection["classification"]

        if classification is None:
            # Detection genuinely failed (no tool call / malformed
            # response) - fails SAFE by proceeding into the rest of the
            # pipeline rather than silently treating an un-classifiable
            # record as healthy, which would be the one failure mode that
            # can never be caught later (a record dropped here is never
            # seen again). This is the opposite default from diagnosis's
            # failure path (which flags for manual review) because the
            # two failures have different costs: an un-diagnosable record
            # still gets a human's attention either way, but a record
            # silently cleared by a failed detection call would vanish
            # with no trace at all.
            audit.log(
                "detection_failed",
                note=detection["reasoning"],
                fallback_classification=NEEDS_ATTENTION,
                **id_kwargs,
            )
        else:
            detection_classification = classification
            # Scored against what the pipeline actually DOES with this
            # classification, not against a literal string match - a
            # hallucinated/non-enum classification value falls through the
            # `== LEAVE_ALONE` check below exactly like NEEDS_ATTENTION
            # does (fails safe by proceeding), so it must score the same
            # way here too. Scoring on `== NEEDS_ATTENTION` alone would
            # silently log a false "matched" for a healthy record whose
            # garbage classification still let it proceed and waste a real
            # manual-review call - see test_generate_detection_pool.py's
            # note above and tests/test_detection_pipeline.py's
            # non-enum-classification test for the case this closes.
            record_proceeds = classification != LEAVE_ALONE
            detection_matched_ground_truth = (
                record_proceeds == ground_truth_needs_attention
                if ground_truth_needs_attention is not None else None
            )
            audit.log(
                "detection_decision",
                classification=classification,
                reasoning=detection["reasoning"],
                ground_truth_needs_attention=ground_truth_needs_attention,
                detection_matched_ground_truth=detection_matched_ground_truth,
                previous_retry_count=record.get("previous_retry_count", 0),
                days_since_last_successful_charge=record.get("days_since_last_successful_charge", 0),
                subscription_status=record.get("subscription_status", "unknown"),
                **id_kwargs,
            )

            if classification == LEAVE_ALONE:
                # The real consequence of a "leave_alone" call: this
                # record NEVER reaches diagnosis, the action proposal, or
                # the gate - if this record was actually at-risk (a wrong
                # detection call), that revenue is genuinely never
                # attempted, not just mislabeled. See
                # tests/test_detection_pipeline.py for the proof.
                return {
                    **id_kwargs,
                    "amount_paise": record.get("amount_paise"),
                    "decline_code": record.get("decline_code"),
                    "diagnosed_decline_code": None,
                    "diagnosis_matched_ground_truth": None,
                    "detection_classification": classification,
                    "detection_matched_ground_truth": detection_matched_ground_truth,
                    "final_action": LEFT_ALONE_BY_DETECTION,
                    "gate_executed": False,
                    "llm_matched_policy": None,
                    "simulated_customer_response": False,
                    "tool_result": None,
                }
            # classification == NEEDS_ATTENTION: fall through below. If
            # this record was actually healthy (a wrong detection call),
            # it now genuinely proceeds into diagnosis/the gate exactly
            # like a real at-risk record would - a real, measurable
            # consequence (wasted diagnosis/gate work, and typically a
            # real flag_for_manual_review tool call on a healthy
            # customer, since a healthy record has no decline_code for
            # the policy table to match), not a cosmetic mislabel.

    # An unrecognized decline code is a real, distinct failure mode from
    # "the LLM proposed something wrong" - it means this record describes
    # a situation the policy table has no entry for at all, so there is no
    # ground truth to gate against and no safe automated action to take.
    # Flagged for manual review explicitly (a human should decide what an
    # unknown code means), never reaching the LLM or the gate at all, since
    # neither has anything to evaluate. See METRICS.md §2.4 and
    # tests/test_agent_unknown_code.py. Applies identically to both
    # domains - agent_onetime.py previously had no equivalent check at all
    # (a real gap this extraction also closes, not just moves).
    if inject_failure == "unknown_decline_code" or record["decline_code"] not in DECLINE_CODES:
        reason = f"No policy entry for decline_code '{record['decline_code']}' - needs human review."
        audit.log(
            "unknown_decline_code",
            decline_code=record["decline_code"],
            fallback_action=RecoveryAction.NO_ACTION_UNRECOVERABLE.value,
            **id_kwargs,
        )
        call = await client.call_tool(
            "flag_for_manual_review", {"subscription_id": record_id, "reason": reason}
        )
        tool_result = extract_tool_text(call)
        audit.log("mcp_tool_call", tool="flag_for_manual_review", result=tool_result, **id_kwargs)
        return {
            **id_kwargs,
            "amount_paise": record["amount_paise"],
            "decline_code": record["decline_code"],
            "diagnosed_decline_code": None,
            "diagnosis_matched_ground_truth": None,
            "detection_classification": detection_classification,
            "detection_matched_ground_truth": detection_matched_ground_truth,
            "final_action": RecoveryAction.NO_ACTION_UNRECOVERABLE.value,
            "gate_executed": True,
            "llm_matched_policy": False,
            "simulated_customer_response": False,
            "tool_result": tool_result,
        }

    # Root-cause diagnosis: infer decline_code from ONLY the raw signal,
    # never from record["decline_code"] itself. `effective_code` is what
    # everything downstream (the action proposal's prompt, gate.evaluate(),
    # execution) actually acts on - ground truth is read here only to
    # compute `diagnosis_matched_ground_truth` for the audit log, exactly
    # as llm_matched_policy measures the action proposal without feeding
    # back into it.
    raw_signal = record.get(raw_signal_field) if raw_signal_field else None
    diagnosed_code: str | None = None
    diagnosis_matched_ground_truth: bool | None = None

    if raw_signal:
        if inject_failure == "diagnosis_parse_failure":
            diagnosis = {
                "decline_code": None,
                "reasoning": "[injected for demo] simulated: model returned no usable diagnosis tool call",
            }
        else:
            diagnosis = diagnose_decline_code(raw_signal, amount_paise=record.get("amount_paise"))
        diagnosed_code = diagnosis["decline_code"]
        diagnosis_matched_ground_truth = diagnosed_code == record["decline_code"]

        if diagnosed_code is None or diagnosed_code not in DECLINE_CODES:
            audit.log(
                "diagnosis_failed",
                raw_decline_message=raw_signal,
                raw_diagnosed_code=diagnosed_code,
                note=diagnosis["reasoning"],
                true_decline_code=record["decline_code"],
                fallback_action=RecoveryAction.NO_ACTION_UNRECOVERABLE.value,
                **id_kwargs,
            )
            reason = (
                f"Diagnosis failed or returned an unrecognized code "
                f"({diagnosed_code!r}) from raw signal - needs human review."
            )
            call = await client.call_tool(
                "flag_for_manual_review", {"subscription_id": record_id, "reason": reason}
            )
            tool_result = extract_tool_text(call)
            audit.log("mcp_tool_call", tool="flag_for_manual_review", result=tool_result, **id_kwargs)
            return {
                **id_kwargs,
                "amount_paise": record["amount_paise"],
                "decline_code": record["decline_code"],
                "diagnosed_decline_code": diagnosed_code,
                "diagnosis_matched_ground_truth": False,
                "detection_classification": detection_classification,
                "detection_matched_ground_truth": detection_matched_ground_truth,
                "final_action": RecoveryAction.NO_ACTION_UNRECOVERABLE.value,
                "gate_executed": True,
                "llm_matched_policy": False,
                "simulated_customer_response": False,
                "tool_result": tool_result,
            }

        audit.log(
            "diagnosis",
            raw_decline_message=raw_signal,
            diagnosed_decline_code=diagnosed_code,
            true_decline_code=record["decline_code"],
            diagnosis_matched_ground_truth=diagnosis_matched_ground_truth,
            diagnosis_reasoning=diagnosis["reasoning"],
            **id_kwargs,
        )

    effective_code = diagnosed_code if raw_signal else record["decline_code"]
    policy = get_decline_code(effective_code)

    # Deterministic, on-demand versions of D4 (BUILD_LOG.md §7.3) for live
    # demos - force the exact same graceful-degradation code below that
    # already runs for a real Ollama hiccup, without needing to wait for
    # one or fake it inside ollama_client.py.
    if inject_failure == "llm_parse_failure":
        proposal = {
            "action": None,
            "reasoning": "[injected for demo] simulated: model returned no usable tool call",
        }
    elif inject_failure == "llm_invalid_action":
        proposal = {
            "action": "definitely_not_a_real_action",
            "reasoning": "[injected for demo] simulated: model proposed an action outside the known enum",
        }
    else:
        # propose_action's prompt embeds record["decline_code"] directly -
        # it must see the DIAGNOSED code, never the ground-truth one, or
        # the "intervention selection" stage would silently launder ground
        # truth back in through a side door even after diagnosis ran.
        action_input_record = (
            record if effective_code == record["decline_code"]
            else {**record, "decline_code": effective_code}
        )
        proposal = propose_action(
            action_input_record, policy.description, policy.source.value,
            situation=situation, id_field=id_field, record_label=record_label,
        )
    llm_action_raw = proposal["action"]

    # Demo mode for the compliant-escalation cross-run stopping rule
    # (gate.py MAX_ATTEMPTS_PER_SUBSCRIPTION): forces this record's
    # prior-attempt count to the cap regardless of its real audit-log
    # history, so the escalation can be shown live. The LLM still runs
    # normally above - this only changes what the gate does with its
    # proposal.
    if inject_failure == "repeat_attempts":
        prior_attempt_count = MAX_ATTEMPTS_PER_SUBSCRIPTION

    # Graceful degradation: if the model failed to produce a usable tool
    # call, fall back to the safest possible default rather than crashing
    # or silently skipping the record. This IS the "one failure handled
    # gracefully" moment from the rubric, and it's a real one - it will
    # actually trigger sometimes with a small local model.
    if llm_action_raw is None:
        audit.log(
            "llm_parse_failure",
            note=proposal["reasoning"],
            fallback_action=RecoveryAction.NO_ACTION_UNRECOVERABLE.value,
            **id_kwargs,
        )
        llm_action = RecoveryAction.NO_ACTION_UNRECOVERABLE
    else:
        try:
            llm_action = RecoveryAction(llm_action_raw)
        except ValueError:
            audit.log(
                "llm_invalid_action",
                raw_action=llm_action_raw,
                fallback_action=RecoveryAction.NO_ACTION_UNRECOVERABLE.value,
                **id_kwargs,
            )
            llm_action = RecoveryAction.NO_ACTION_UNRECOVERABLE

    # The gate evaluates the DIAGNOSED code, not ground truth - a wrong
    # diagnosis means the gate looks up the WRONG policy row and can hand
    # back a materially different final_action than ground truth would
    # have produced. That's the whole point: diagnosis has real downstream
    # consequences here, not just a logged-and-ignored side note.
    decision = gate.evaluate(
        subscription_id=record_id,
        decline_code=effective_code,
        proposed_action=llm_action,
        amount_paise=record["amount_paise"],
        prior_attempt_count=prior_attempt_count,
        halted_days_ago=record.get("halted_days_ago"),
    )

    audit.log(
        "gate_decision",
        decline_code=effective_code,
        true_decline_code=record["decline_code"],
        diagnosed_decline_code=diagnosed_code,
        diagnosis_matched_ground_truth=diagnosis_matched_ground_truth,
        llm_proposed_action=llm_action.value,
        llm_reasoning=proposal["reasoning"],
        llm_matched_policy=decision.llm_matched_policy,
        gate_execute=decision.execute,
        gate_reason=decision.reason,
        final_action=decision.final_action.value,
        prior_attempt_count=prior_attempt_count,
        **id_kwargs,
    )

    tool_result = None
    if decision.execute and decision.final_action in (
        RecoveryAction.IMMEDIATE_RETRY,
        RecoveryAction.DELAYED_RETRY,
        RecoveryAction.PAYMENT_LINK_NUDGE,
    ):
        tool_name = action_to_tool[decision.final_action]
        if tool_name == "create_payment_link":
            args = {
                "subscription_id": record_id,
                "amount_paise": record["amount_paise"],
                "description": f"Complete your payment for {record[item_label_field]}",
            }
        else:
            args = {"subscription_id": record_id, "amount_paise": record["amount_paise"]}
        call = await client.call_tool(tool_name, args)
        tool_result = extract_tool_text(call)
        audit.log("mcp_tool_call", tool=tool_name, arguments=args, result=tool_result, **id_kwargs)
    elif decision.final_action in (RecoveryAction.NO_ACTION_FRAUD, RecoveryAction.NO_ACTION_UNRECOVERABLE):
        call = await client.call_tool(
            "flag_for_manual_review",
            {"subscription_id": record_id, "reason": decision.reason},
        )
        tool_result = extract_tool_text(call)
        audit.log("mcp_tool_call", tool="flag_for_manual_review", result=tool_result, **id_kwargs)

    return {
        **id_kwargs,
        "amount_paise": record["amount_paise"],
        "decline_code": record["decline_code"],
        "diagnosed_decline_code": diagnosed_code,
        "diagnosis_matched_ground_truth": diagnosis_matched_ground_truth,
        "detection_classification": detection_classification,
        "detection_matched_ground_truth": detection_matched_ground_truth,
        "final_action": decision.final_action.value,
        "gate_executed": decision.execute,
        "llm_matched_policy": decision.llm_matched_policy,
        "simulated_customer_response": record["simulated_customer_response"],
        "tool_result": tool_result,
    }


def render_decline_code_table(results: list[dict]) -> list[str]:
    """
    Shared '## By decline code' markdown table - byte-identical section
    that used to be duplicated between agent.py's write_results() and
    agent_onetime.py's write_results().
    """
    lines = [
        "## By decline code",
        "",
        "| Decline code | Count | Final action |",
        "|---|---|---|",
    ]
    seen: dict[tuple[str, str], int] = {}
    for r in results:
        key = (r["decline_code"], r["final_action"])
        seen[key] = seen.get(key, 0) + 1
    for (code, action), count in sorted(seen.items()):
        lines.append(f"| {code} | {count} | {action} |")
    return lines
