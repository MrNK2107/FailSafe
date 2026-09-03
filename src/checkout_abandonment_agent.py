"""
Standalone pipeline for CHECKOUT ABANDONMENT recovery - closes one of the
two previously-undisclosed category-scope gaps this project's own honest
self-audit found (PS_REQUIREMENTS_DEBATE.md; README.md §6: "checkout
abandonment... [has] zero code, data model, or test anywhere in this
repo"). Overdue receivables (the third named category) remains
untouched - see BUILD_LOG.md's dated entry and README.md §6 for the
honest, unchanged disclosure of that.

Kept as its OWN standalone module, never wired into agent.py,
recovery_pipeline.py, or the 150-record flagship run - mirrors
agent_onetime.py's and route_demo.py's own precedent exactly:
`data/halted_subscriptions.json`, `logs/audit_log.jsonl`, and
`RESULTS.md` are completely untouched by this file. Own data
(`data/abandoned_checkouts.json`), own audit log
(`logs/audit_log_checkout_abandonment.jsonl`), own results file
(`CHECKOUT_ABANDONMENT_RESULTS.md`).

Pipeline shape, and the one deliberate structural difference from the
flagship pipeline, stated honestly rather than silently copied:
diagnose -> gate -> execute -> audit. There is NO separate "LLM proposes
an action, gate checks it against policy" step here, unlike
agent.py/agent_onetime.py's propose_action() -> gate.evaluate() split.
See abandonment_gate.py's module docstring for the full reasoning,
summarized here: once a reason is diagnosed, the action is a
deterministic function of that reason via
config/abandonment_policy.json (checkout_abandonment_policy.py) - there
is no second, independent judgment call left for a model to get right or
wrong, so a second LLM call would roughly double latency per record (as
diagnosis_live_demo.py's docstring found for the analogous
diagnosis+action-proposal case in the flagship pipeline) without adding a
genuinely separate decision for the gate to police. The single genuinely
fallible, AI-driven judgment call in this domain is entirely in diagnosis
- which abandonment_reason explains this session - exactly where this
domain's required "genuine reasoning stage" lives, and getting THAT wrong
is what has real downstream consequences (wrong reason -> wrong policy
row -> wrong action), proven directly by
tests/test_checkout_abandonment_pipeline.py's load-bearing test,
mirroring
test_wrong_diagnosis_changes_the_final_action_real_downstream_consequences
in tests/test_diagnosis_pipeline.py.

Routes recovery actions through the REAL existing MCP server
(mcp_server.py) - create_payment_link for every reason-driven action (a
"send the customer a fresh way to complete this checkout" call fits all
four actionable reasons; only the message framing differs by action, not
the underlying Razorpay operation - no new MCP tool was needed or
invented) and flag_for_manual_review for every no-action outcome,
mirroring exactly how the flagship pipeline calls flag_for_manual_review
for every no-action policy. The tool's `subscription_id` parameter name
is reused verbatim for `cart_id` here - the same generic-ID convention
recovery_pipeline.py already uses for agent_onetime.py's `payment_id`
records, not a new pattern.

SIMULATE is forced True for THIS script's run(), unlike agent.py/
agent_onetime.py (which let real `.env` keys flow through to the real
official MCP server if present). Found the hard way, not assumed: this
repo's own `.env` already has real `rzp_test_` keys configured (from
earlier real-key demos - see README.md §6/BUILD_LOG.md), and this
project's own real test-mode account has already exhausted its
payment-link quota (README.md §6's "test mode limit of 30 reached"
disclosure) - so letting a fresh domain's live demo route through the
real official MCP server here would either fail outright or consume
scarce real-account quota for a script whose entire purpose is measuring
DIAGNOSIS behavior, not creating real Razorpay objects. Same reasoning,
and the same two-patch fix, as `diagnosis_live_demo.py`/
`detection_live_demo.py`: patching only `mcp_server.SIMULATE` is NOT
sufficient, since `mcp_server._rp` is constructed once at import time and
branches on its own bound `self.simulate` - both must be patched. Direct
programmatic use of `process_one()` (e.g. from a test) is unaffected and
still respects whatever `.env`/SIMULATE state is active at call time.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from mcp import Client

import mcp_server as _mcp_server_module

from abandonment_gate import AbandonmentGate
from audit_log import AuditLogger
from checkout_abandonment_policy import AbandonmentAction, get_abandonment_policy
from diagnose_checkout_abandonment import diagnose_abandonment_reason
from mcp_server import server as mcp_server

DATA_PATH = Path(__file__).parent.parent / "data" / "abandoned_checkouts.json"
AUDIT_PATH = Path(__file__).parent.parent / "logs" / "audit_log_checkout_abandonment.jsonl"
RESULTS_PATH = Path(__file__).parent.parent / "CHECKOUT_ABANDONMENT_RESULTS.md"

ID_FIELD = "cart_id"

ACTION_TO_DESCRIPTION = {
    AbandonmentAction.IMMEDIATE_PAYMENT_LINK_RESEND:
        "Complete your payment for {item} - pick up right where you left off.",
    AbandonmentAction.PAYMENT_LINK_ALTERNATE_METHODS_NUDGE:
        "Complete your payment for {item} - now with more payment methods to choose from.",
    AbandonmentAction.DISCOUNTED_INCENTIVE_NUDGE:
        "Complete your payment for {item} - here's a little something extra to say thanks for coming back.",
    AbandonmentAction.DELAYED_NUDGE_NO_DISCOUNT:
        "Still interested? Complete your payment for {item} whenever you're ready.",
}
ACTIONABLE = set(ACTION_TO_DESCRIPTION)
NO_ACTION = {
    AbandonmentAction.NO_ACTION_RESPECT_HESITATION,
    AbandonmentAction.NO_ACTION_LOW_VALUE,
    AbandonmentAction.NO_ACTION_STALE_ABANDONMENT,
    AbandonmentAction.NO_ACTION_NEEDS_HUMAN_REVIEW,
}


def _valid_reason(reason) -> bool:
    from checkout_abandonment_policy import AbandonmentReason
    return reason in {r.value for r in AbandonmentReason}


def extract_tool_text(call) -> str | None:
    if not call.content:
        return None
    first = call.content[0]
    return getattr(first, "text", None)


async def process_one(
    client: Client,
    gate: AbandonmentGate,
    audit: AuditLogger,
    cart: dict,
    inject_failure: str | None = None,
) -> dict:
    """
    One abandoned cart through diagnose -> gate -> execute -> audit. See
    module docstring for why there is no separate action-proposal LLM
    stage in this domain.
    """
    cart_id = cart[ID_FIELD]

    if inject_failure == "diagnosis_parse_failure":
        diagnosis = {
            "reason": None,
            "reasoning": "[injected for demo] simulated: model returned no usable tool call",
        }
    else:
        diagnosis = diagnose_abandonment_reason(
            checkout_stage=cart["checkout_stage"],
            minutes_since_abandonment=cart["minutes_since_abandonment"],
            device_type=cart["device_type"],
            is_returning_customer=cart["is_returning_customer"],
            amount_paise=cart.get("amount_paise"),
        )
    diagnosed_reason = diagnosis["reason"]
    diagnosis_matched_ground_truth = diagnosed_reason == cart["abandonment_reason"]

    if diagnosed_reason is None or not _valid_reason(diagnosed_reason):
        audit.log(
            "abandonment_diagnosis_failed",
            cart_id=cart_id,
            raw_diagnosed_reason=diagnosed_reason,
            note=diagnosis["reasoning"],
            true_reason=cart["abandonment_reason"],
            fallback_action=AbandonmentAction.NO_ACTION_NEEDS_HUMAN_REVIEW.value,
        )
        review_reason = (
            f"Abandonment diagnosis failed or returned an unrecognized reason "
            f"({diagnosed_reason!r}) - needs human review."
        )
        call = await client.call_tool(
            "flag_for_manual_review", {"subscription_id": cart_id, "reason": review_reason}
        )
        tool_result = extract_tool_text(call)
        audit.log("mcp_tool_call", tool="flag_for_manual_review", result=tool_result, cart_id=cart_id)
        return {
            "cart_id": cart_id,
            "amount_paise": cart["amount_paise"],
            "abandonment_reason": cart["abandonment_reason"],
            "diagnosed_reason": diagnosed_reason,
            "diagnosis_matched_ground_truth": False,
            "final_action": AbandonmentAction.NO_ACTION_NEEDS_HUMAN_REVIEW.value,
            "gate_executed": True,
            "simulated_customer_response": False,
            "tool_result": tool_result,
        }

    audit.log(
        "abandonment_diagnosis",
        cart_id=cart_id,
        diagnosed_reason=diagnosed_reason,
        true_reason=cart["abandonment_reason"],
        diagnosis_matched_ground_truth=diagnosis_matched_ground_truth,
        diagnosis_reasoning=diagnosis["reasoning"],
    )

    decision = gate.evaluate(
        cart_id=cart_id,
        abandonment_reason=diagnosed_reason,
        amount_paise=cart["amount_paise"],
        minutes_since_abandonment=cart["minutes_since_abandonment"],
    )

    audit.log(
        "abandonment_gate_decision",
        cart_id=cart_id,
        diagnosed_reason=diagnosed_reason,
        true_reason=cart["abandonment_reason"],
        diagnosis_matched_ground_truth=diagnosis_matched_ground_truth,
        gate_execute=decision.execute,
        gate_reason=decision.reason,
        final_action=decision.final_action.value,
    )

    tool_result = None
    if decision.execute and decision.final_action in ACTIONABLE:
        description = ACTION_TO_DESCRIPTION[decision.final_action].format(item=cart["item"])
        args = {
            "subscription_id": cart_id,
            "amount_paise": cart["amount_paise"],
            "description": description,
        }
        call = await client.call_tool("create_payment_link", args)
        tool_result = extract_tool_text(call)
        audit.log("mcp_tool_call", tool="create_payment_link", arguments=args, result=tool_result, cart_id=cart_id)
    elif decision.final_action in NO_ACTION:
        call = await client.call_tool(
            "flag_for_manual_review", {"subscription_id": cart_id, "reason": decision.reason}
        )
        tool_result = extract_tool_text(call)
        audit.log("mcp_tool_call", tool="flag_for_manual_review", result=tool_result, cart_id=cart_id)

    return {
        "cart_id": cart_id,
        "amount_paise": cart["amount_paise"],
        "abandonment_reason": cart["abandonment_reason"],
        "diagnosed_reason": diagnosed_reason,
        "diagnosis_matched_ground_truth": diagnosis_matched_ground_truth,
        "final_action": decision.final_action.value,
        "gate_executed": decision.execute,
        "simulated_customer_response": cart["simulated_customer_response"],
        "tool_result": tool_result,
    }


async def run(n: int | None = None):
    if not DATA_PATH.exists():
        raise SystemExit("No data found. Run `python generate_checkout_abandonment_data.py` first.")

    carts = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    if n is not None:
        carts = carts[:n]

    gate = AbandonmentGate()
    audit = AuditLogger(AUDIT_PATH)
    audit.log("run_started", total_carts=len(carts))
    print(f"Processing {len(carts)} abandoned checkouts...")

    results = []
    with patch.object(_mcp_server_module, "SIMULATE", True), \
         patch.object(_mcp_server_module._rp, "simulate", True):
        async with Client(mcp_server) as client:
            results = await _process_all(client, gate, audit, carts)

    audit.log("run_finished", total_processed=len(results))
    write_results(results)
    return results


async def _process_all(client: Client, gate: AbandonmentGate, audit: AuditLogger, carts: list[dict]) -> list[dict]:
    results = []
    for cart in carts:
        try:
            result = await process_one(client, gate, audit, cart)
        except Exception as e:
            audit.log("record_processing_error", cart_id=cart["cart_id"], error=str(e))
            result = {
                "cart_id": cart["cart_id"],
                "amount_paise": cart["amount_paise"],
                "abandonment_reason": cart["abandonment_reason"],
                "diagnosed_reason": None,
                "diagnosis_matched_ground_truth": False,
                "final_action": AbandonmentAction.NO_ACTION_NEEDS_HUMAN_REVIEW.value,
                "gate_executed": False,
                "simulated_customer_response": False,
                "tool_result": None,
            }
        results.append(result)
        match = "MATCH" if result["diagnosis_matched_ground_truth"] else "MISS "
        print(
            f"{cart['cart_id']}  true={cart['abandonment_reason']:28s} "
            f"diagnosed={str(result['diagnosed_reason']):28s} [{match}] -> {result['final_action']}"
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

    # Mirrors diagnosis_live_demo.py's "real downstream consequences"
    # measurement: for every record, what final_action would GROUND
    # TRUTH's own policy have produced, versus what actually happened
    # using the diagnosed reason. Only counts cases where they genuinely
    # differ - a misdiagnosis that happens to land on a reason sharing the
    # same allowed_action as truth has no visible consequence.
    changed_action = 0
    for r in results:
        if r["diagnosed_reason"] is None:
            changed_action += 1  # diagnosis failure always forces a different (review) action
            continue
        true_policy_action = get_abandonment_policy(r["abandonment_reason"]).allowed_action.value
        if not r["diagnosis_matched_ground_truth"] and r["final_action"] != true_policy_action:
            changed_action += 1

    by_action: dict[str, int] = {}
    for r in results:
        by_action[r["final_action"]] = by_action.get(r["final_action"], 0) + 1

    lines = [
        "# CHECKOUT_ABANDONMENT_RESULTS",
        "",
        "A NEW, standalone domain: checkout abandonment (a customer who started",
        "checkout but never completed a payment attempt at all - no decline_code",
        "exists here by definition). Closes one of the two previously-undisclosed",
        "category-scope gaps (PS_REQUIREMENTS_DEBATE.md; README.md §6). Kept",
        "separate from the 150-record flagship pipeline, exactly like",
        "agent_onetime.py and route_demo.py already are - see",
        "checkout_abandonment_agent.py's module docstring for what is and isn't",
        "shared with that pipeline. Same honesty caveat as RESULTS.md/RESULTS_ONETIME.md:",
        "`simulated_customer_response` is a labeled synthetic assumption, not a real",
        "customer outcome.",
        "",
        f"- Abandoned checkouts processed: **{total}**",
        f"- Total abandoned cart value: **Rs {total_paise/100:,.2f}**",
        f"- **Diagnosis accuracy (diagnosed_reason == true abandonment_reason): "
        f"{matched}/{total} ({matched/total*100:.1f}%)**",
        f"- Misdiagnoses that changed the final action versus what ground truth's own "
        f"policy would have given: **{changed_action}/{total}** - the concrete proof "
        "that a wrong diagnosis has real downstream consequences here too, mirroring "
        "DIAGNOSIS_DEMO_RESULTS.md's own measurement.",
        f"- Actions executed (payment-link nudges the gate let through): **{len(acted_on)}**",
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
        "## Per-cart detail",
        "",
        "| Cart | True reason | Diagnosed reason | Match | Final action |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['cart_id']} | {r['abandonment_reason']} | {r['diagnosed_reason']} | "
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
