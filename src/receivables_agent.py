"""
Standalone pipeline for OVERDUE RECEIVABLES recovery - closes the LAST of
the three category-scope gaps this project's own honest self-audit found
(PS_REQUIREMENTS_DEBATE.md; README.md §6: "overdue receivables... still
has zero code, data model, or test anywhere in this repo"). All three
revenue-loss categories Track 3's problem statement names - payment
failures, checkout abandonment, overdue receivables - now have some real,
tested implementation in this repo.

Kept as its OWN standalone module, never wired into agent.py,
checkout_abandonment_agent.py, recovery_pipeline.py, or the 150-record
flagship run - mirrors agent_onetime.py's/route_demo.py's/
checkout_abandonment_agent.py's own precedent exactly:
`data/halted_subscriptions.json`, `data/abandoned_checkouts.json`,
`logs/audit_log.jsonl`, `logs/audit_log_checkout_abandonment.jsonl`,
`RESULTS.md`, and `CHECKOUT_ABANDONMENT_RESULTS.md` are all completely
untouched by this file. Own data (`data/overdue_invoices.json`), own
audit log (`logs/audit_log_receivables.jsonl`), own results file
(`RECEIVABLES_RESULTS.md`).

Pipeline shape, and the one deliberate structural difference from the
flagship pipeline (same choice checkout_abandonment_agent.py already made,
for the same reason): diagnose -> gate -> execute -> audit. There is NO
separate "LLM proposes an action, gate checks it against policy" step
here - once a case_reason is diagnosed, the action is a deterministic
function of that reason via config/receivables_policy.json
(receivables_policy.py). The single genuinely fallible, AI-driven
judgment call in this domain is entirely in diagnosis - which
case_reason explains why this invoice is overdue - exactly where this
domain's required "genuine reasoning stage" lives, and getting THAT wrong
is what has real downstream consequences (wrong reason -> wrong policy
row -> wrong action), proven directly by
tests/test_receivables_pipeline.py's load-bearing test, mirroring
test_wrong_diagnosis_changes_the_final_action_real_downstream_consequences
in tests/test_diagnosis_pipeline.py and
tests/test_checkout_abandonment_pipeline.py.

Routes recovery actions through the REAL existing MCP server
(mcp_server.py) - create_payment_link for every reminder/payment-plan-
offer action (a "send the customer/business a fresh link and message"
call fits all three actionable case reasons; only the message framing
differs by action, not the underlying Razorpay operation - no new MCP
tool was needed or invented) and flag_for_manual_review for every
no-action/escalation outcome (dispute review, manual-collections
escalation, the two compliant-escalation stopping rules, and the hard-
block fallback), mirroring exactly how the other two domains call
flag_for_manual_review for every no-action policy. The tool's
`subscription_id` parameter name is reused verbatim for `invoice_id`
here - the same generic-ID convention recovery_pipeline.py already uses
for agent_onetime.py's `payment_id` records and
checkout_abandonment_agent.py's own `cart_id` records, not a new pattern.

SIMULATE is forced True for THIS script's run(), for the exact same
reason and with the exact same two-patch fix (`mcp_server.SIMULATE` alone
is not sufficient, since `mcp_server._rp` is constructed once at import
time and branches on its own bound `self.simulate`) diagnosis_live_demo.py/
detection_live_demo.py/checkout_abandonment_agent.py already needed and
documented: this repo's own `.env` already has real `rzp_test_` keys
configured, and this project's own real test-mode account has already
exhausted its payment-link quota - so a fresh domain's live demo must not
route through the real official MCP server or consume scarce real-account
quota for a script whose entire purpose is measuring DIAGNOSIS behavior.
Direct programmatic use of `process_one()` (e.g. from a test) is
unaffected and still respects whatever `.env`/SIMULATE state is active at
call time.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from mcp import Client

import mcp_server as _mcp_server_module

from audit_log import AuditLogger
from diagnose_receivable import diagnose_receivable
from mcp_server import server as mcp_server
from receivables_gate import ReceivableGate
from receivables_policy import ReceivableAction, get_receivable_policy

DATA_PATH = Path(__file__).parent.parent / "data" / "overdue_invoices.json"
AUDIT_PATH = Path(__file__).parent.parent / "logs" / "audit_log_receivables.jsonl"
RESULTS_PATH = Path(__file__).parent.parent / "RECEIVABLES_RESULTS.md"

ID_FIELD = "invoice_id"

ACTION_TO_DESCRIPTION = {
    ReceivableAction.FRIENDLY_REMINDER:
        "Friendly reminder: invoice for {business_name} is overdue - here is a fresh link to settle it whenever convenient.",
    ReceivableAction.PAYMENT_PLAN_OFFER:
        "We understand timing can be tight - here is a link to settle the overdue invoice for {business_name} via an installment plan.",
    ReceivableAction.FIRM_REMINDER_WITH_DEADLINE:
        "This invoice for {business_name} is now overdue - please settle it via the link below within 7 days to avoid further escalation.",
}
ACTIONABLE = set(ACTION_TO_DESCRIPTION)
NO_ACTION = {
    ReceivableAction.NO_ACTION_NEEDS_DISPUTE_REVIEW,
    ReceivableAction.ESCALATE_TO_MANUAL_COLLECTIONS,
    ReceivableAction.NO_ACTION_ALREADY_ESCALATED,
    ReceivableAction.NO_ACTION_STALE_INVOICE_NEEDS_LEGAL_REVIEW,
    ReceivableAction.NO_ACTION_NEEDS_HUMAN_REVIEW,
}


def _valid_case_reason(reason) -> bool:
    from receivables_policy import ReceivableReason
    return reason in {r.value for r in ReceivableReason}


def extract_tool_text(call) -> str | None:
    if not call.content:
        return None
    first = call.content[0]
    return getattr(first, "text", None)


async def process_one(
    client: Client,
    gate: ReceivableGate,
    audit: AuditLogger,
    invoice: dict,
    inject_failure: str | None = None,
) -> dict:
    """
    One overdue invoice through diagnose -> gate -> execute -> audit. See
    module docstring for why there is no separate action-proposal LLM
    stage in this domain.
    """
    invoice_id = invoice[ID_FIELD]

    if inject_failure == "diagnosis_parse_failure":
        diagnosis = {
            "case_reason": None,
            "reasoning": "[injected for demo] simulated: model returned no usable tool call",
        }
    else:
        diagnosis = diagnose_receivable(
            days_overdue=invoice["days_overdue"],
            payment_terms=invoice["payment_terms"],
            customer_payment_history_signal=invoice["customer_payment_history_signal"],
            reminders_sent_count=invoice["reminders_sent_count"],
            last_reminder_response=invoice.get("last_reminder_response"),
            amount_vs_typical_ratio=invoice.get("amount_vs_typical_ratio"),
        )
    diagnosed_case_reason = diagnosis["case_reason"]
    diagnosis_matched_ground_truth = diagnosed_case_reason == invoice["case_reason"]

    if diagnosed_case_reason is None or not _valid_case_reason(diagnosed_case_reason):
        audit.log(
            "receivable_diagnosis_failed",
            invoice_id=invoice_id,
            raw_diagnosed_case_reason=diagnosed_case_reason,
            note=diagnosis["reasoning"],
            true_case_reason=invoice["case_reason"],
            fallback_action=ReceivableAction.NO_ACTION_NEEDS_HUMAN_REVIEW.value,
        )
        review_reason = (
            f"Receivables diagnosis failed or returned an unrecognized case reason "
            f"({diagnosed_case_reason!r}) - needs human review."
        )
        call = await client.call_tool(
            "flag_for_manual_review", {"subscription_id": invoice_id, "reason": review_reason}
        )
        tool_result = extract_tool_text(call)
        audit.log("mcp_tool_call", tool="flag_for_manual_review", result=tool_result, invoice_id=invoice_id)
        return {
            "invoice_id": invoice_id,
            "amount_paise": invoice["amount_paise"],
            "case_reason": invoice["case_reason"],
            "diagnosed_case_reason": diagnosed_case_reason,
            "diagnosis_matched_ground_truth": False,
            "final_action": ReceivableAction.NO_ACTION_NEEDS_HUMAN_REVIEW.value,
            "gate_executed": True,
            "simulated_customer_response": False,
            "tool_result": tool_result,
        }

    audit.log(
        "receivable_diagnosis",
        invoice_id=invoice_id,
        diagnosed_case_reason=diagnosed_case_reason,
        true_case_reason=invoice["case_reason"],
        diagnosis_matched_ground_truth=diagnosis_matched_ground_truth,
        diagnosis_reasoning=diagnosis["reasoning"],
    )

    decision = gate.evaluate(
        invoice_id=invoice_id,
        case_reason=diagnosed_case_reason,
        amount_paise=invoice["amount_paise"],
        days_overdue=invoice["days_overdue"],
        reminders_sent_count=invoice["reminders_sent_count"],
    )

    audit.log(
        "receivable_gate_decision",
        invoice_id=invoice_id,
        diagnosed_case_reason=diagnosed_case_reason,
        true_case_reason=invoice["case_reason"],
        diagnosis_matched_ground_truth=diagnosis_matched_ground_truth,
        gate_execute=decision.execute,
        gate_reason=decision.reason,
        final_action=decision.final_action.value,
    )

    tool_result = None
    if decision.execute and decision.final_action in ACTIONABLE:
        description = ACTION_TO_DESCRIPTION[decision.final_action].format(
            business_name=invoice["business_name"]
        )
        args = {
            "subscription_id": invoice_id,
            "amount_paise": invoice["amount_paise"],
            "description": description,
        }
        call = await client.call_tool("create_payment_link", args)
        tool_result = extract_tool_text(call)
        audit.log("mcp_tool_call", tool="create_payment_link", arguments=args, result=tool_result, invoice_id=invoice_id)
    elif decision.final_action in NO_ACTION:
        call = await client.call_tool(
            "flag_for_manual_review", {"subscription_id": invoice_id, "reason": decision.reason}
        )
        tool_result = extract_tool_text(call)
        audit.log("mcp_tool_call", tool="flag_for_manual_review", result=tool_result, invoice_id=invoice_id)

    return {
        "invoice_id": invoice_id,
        "amount_paise": invoice["amount_paise"],
        "case_reason": invoice["case_reason"],
        "diagnosed_case_reason": diagnosed_case_reason,
        "diagnosis_matched_ground_truth": diagnosis_matched_ground_truth,
        "final_action": decision.final_action.value,
        "gate_executed": decision.execute,
        "simulated_customer_response": invoice["simulated_customer_response"],
        "tool_result": tool_result,
    }


async def run(n: int | None = None):
    if not DATA_PATH.exists():
        raise SystemExit("No data found. Run `python generate_receivables_data.py` first.")

    invoices = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    if n is not None:
        invoices = invoices[:n]

    gate = ReceivableGate()
    audit = AuditLogger(AUDIT_PATH)
    audit.log("run_started", total_invoices=len(invoices))
    print(f"Processing {len(invoices)} overdue invoices...")

    with patch.object(_mcp_server_module, "SIMULATE", True), \
         patch.object(_mcp_server_module._rp, "simulate", True):
        async with Client(mcp_server) as client:
            results = await _process_all(client, gate, audit, invoices)

    audit.log("run_finished", total_processed=len(results))
    write_results(results)
    return results


async def _process_all(client: Client, gate: ReceivableGate, audit: AuditLogger, invoices: list[dict]) -> list[dict]:
    results = []
    for invoice in invoices:
        try:
            result = await process_one(client, gate, audit, invoice)
        except Exception as e:
            audit.log("record_processing_error", invoice_id=invoice["invoice_id"], error=str(e))
            result = {
                "invoice_id": invoice["invoice_id"],
                "amount_paise": invoice["amount_paise"],
                "case_reason": invoice["case_reason"],
                "diagnosed_case_reason": None,
                "diagnosis_matched_ground_truth": False,
                "final_action": ReceivableAction.NO_ACTION_NEEDS_HUMAN_REVIEW.value,
                "gate_executed": False,
                "simulated_customer_response": False,
                "tool_result": None,
            }
        results.append(result)
        match = "MATCH" if result["diagnosis_matched_ground_truth"] else "MISS "
        print(
            f"{invoice['invoice_id']}  true={invoice['case_reason']:40s} "
            f"diagnosed={str(result['diagnosed_case_reason']):40s} [{match}] -> {result['final_action']}"
        )
    return results


def write_results(results: list[dict]):
    total = len(results)
    matched = sum(1 for r in results if r["diagnosis_matched_ground_truth"])
    actionable_values = {a.value for a in ACTIONABLE}
    acted_on = [r for r in results if r["final_action"] in actionable_values and r["gate_executed"]]
    recovered = [r for r in acted_on if r["simulated_customer_response"]]
    total_paise = sum(r["amount_paise"] for r in results)
    recovered_paise = sum(r["amount_paise"] for r in recovered)

    # Mirrors diagnosis_live_demo.py's/checkout_abandonment_agent.py's own
    # "real downstream consequences" measurement: for every record, what
    # final_action would GROUND TRUTH's own policy have produced, versus
    # what actually happened using the diagnosed case_reason. Only counts
    # cases where they genuinely differ.
    changed_action = 0
    for r in results:
        if r["diagnosed_case_reason"] is None:
            changed_action += 1  # diagnosis failure always forces a different (review) action
            continue
        true_policy_action = get_receivable_policy(r["case_reason"]).allowed_action.value
        if not r["diagnosis_matched_ground_truth"] and r["final_action"] != true_policy_action:
            changed_action += 1

    by_action: dict[str, int] = {}
    for r in results:
        by_action[r["final_action"]] = by_action.get(r["final_action"], 0) + 1

    lines = [
        "# RECEIVABLES_RESULTS",
        "",
        "A NEW, standalone domain: overdue receivables (a B2B invoice that has",
        "gone unpaid past its due date - no decline_code and no checkout funnel",
        "exist here by definition; this domain revolves around an aging clock,",
        "`days_overdue`, plus a business's own payment-history and communication",
        "signals). Closes the LAST of the three category-scope gaps",
        "(PS_REQUIREMENTS_DEBATE.md; README.md §6). Kept separate from the",
        "150-record flagship pipeline and from checkout_abandonment_agent.py,",
        "exactly like agent_onetime.py/route_demo.py already are - see",
        "receivables_agent.py's module docstring for what is and isn't shared",
        "with those pipelines. Same honesty caveat as RESULTS.md/",
        "CHECKOUT_ABANDONMENT_RESULTS.md: `simulated_customer_response` is a",
        "labeled synthetic assumption, not a real customer outcome.",
        "",
        f"- Overdue invoices processed: **{total}**",
        f"- Total overdue invoice value: **Rs {total_paise/100:,.2f}**",
        f"- **Diagnosis accuracy (diagnosed_case_reason == true case_reason): "
        f"{matched}/{total} ({matched/total*100:.1f}%)**",
        f"- Misdiagnoses that changed the final action versus what ground truth's own "
        f"policy would have given: **{changed_action}/{total}** - the concrete proof "
        "that a wrong diagnosis has real downstream consequences here too, mirroring "
        "DIAGNOSIS_DEMO_RESULTS.md's/CHECKOUT_ABANDONMENT_RESULTS.md's own measurement.",
        f"- Actions executed (reminders/payment-plan offers/escalations the gate let through): **{len(acted_on)}**",
        f"- Simulated recovered amount: **Rs {recovered_paise/100:,.2f}** "
        f"({len(recovered)}/{len(acted_on)} executed actions 'succeeded' in simulation)",
        "",
        "## By final action",
        "",
        "| Final action | Count |",
        "|---|---|",
    ]
    for action, count in sorted(by_action.items()):
        lines.append(f"| {action} | {count} |")

    lines += [
        "",
        "## Per-invoice detail",
        "",
        "| Invoice | True reason | Diagnosed reason | Match | Final action |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['invoice_id']} | {r['case_reason']} | {r['diagnosed_case_reason']} | "
            f"{'yes' if r['diagnosis_matched_ground_truth'] else 'no'} | {r['final_action']} |"
        )

    RESULTS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {RESULTS_PATH}")
    print(f"\nDiagnosis accuracy: {matched}/{total} ({matched/total*100:.1f}%)")
    print(f"Final-action changed by misdiagnosis: {changed_action}/{total}")


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    asyncio.run(run(n))
