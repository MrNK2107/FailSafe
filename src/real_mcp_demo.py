"""
Runs a handful of real halted-subscription records end-to-end through the
REAL Razorpay official MCP server (razorpay/razorpay-mcp-server, via
razorpay_mcp_client.py), using real rzp_test_ keys from .env - not the
in-process simulate mode every other run in this repo has used so far
(BUILD_LOG.md §12 flagged this as the one integration claim that was
implemented and probed but never actually exercised end-to-end; this
script closes that gap).

Deliberately separate from agent.py's main 150-record pipeline:
  - Writes to logs/real_mcp_server_run.jsonl and REAL_MCP_RESULTS.md, not
    logs/audit_log.jsonl / results_checkpoint.jsonl, so it can run without
    colliding with (or resetting) the main pipeline's checkpoint state.
  - Uses each record's own correct policy action directly (via
    decline_codes.py) instead of calling the local Ollama model - this
    script exists to prove the MCP integration executes real Razorpay
    calls correctly, not to re-test LLM proposal quality (already covered
    extensively by agent.py's runs and METRICS.md). The gate still runs
    for real on every record - policy match, spending cap, idempotency -
    exactly as it would for an LLM proposal.

Usage: python real_mcp_demo.py [N]   (default N=5)
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import Client

from decline_codes import RecoveryAction, get_decline_code
from gate import Gate
from mcp_server import server as mcp_server
from razorpay_client import SIMULATE

DATA_PATH = Path(__file__).parent.parent / "data" / "halted_subscriptions.json"
LOG_PATH = Path(__file__).parent.parent / "logs" / "real_mcp_server_run.jsonl"
RESULTS_PATH = Path(__file__).parent.parent / "REAL_MCP_RESULTS.md"

ACTION_TO_TOOL = {
    RecoveryAction.IMMEDIATE_RETRY: "create_retry_order",
    RecoveryAction.DELAYED_RETRY: "create_retry_order",
    RecoveryAction.PAYMENT_LINK_NUDGE: "create_payment_link",
}


def _pick_sample(subscriptions: list[dict], n: int) -> list[dict]:
    """Pick a small, decline-code-diverse sample covering both
    money-moving actions (retry and payment-link), not just the first N
    records in file order, so this demo actually exercises both real
    tools rather than N copies of the same one."""
    wanted_codes = [
        "payment_timed_out",       # immediate_retry
        "gateway_technical_error", # immediate_retry
        "insufficient_funds",      # delayed_retry
        "card_expired",            # payment_link_nudge
        "authentication_failed",   # payment_link_nudge
    ]
    by_code = {}
    for s in subscriptions:
        by_code.setdefault(s["decline_code"], s)
    sample = [by_code[c] for c in wanted_codes if c in by_code]
    return sample[:n]


def _log(entries: list[dict], **fields):
    entries.append(fields)


async def run(n: int = 5):
    if SIMULATE:
        raise SystemExit(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set to real rzp_test_ "
            "keys in .env - this script requires them (the official MCP server "
            "has no simulate mode). See .env.example."
        )
    if not DATA_PATH.exists():
        raise SystemExit("No data found. Run generate_data.py first.")

    subscriptions = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    sample = _pick_sample(subscriptions, n)
    gate = Gate()
    log_entries: list[dict] = []

    print(f"Running {len(sample)} records through the REAL Razorpay official MCP server (test mode)...\n")

    async with Client(mcp_server) as client:
        for sub in sample:
            policy = get_decline_code(sub["decline_code"])
            decision = gate.evaluate(
                subscription_id=sub["subscription_id"],
                decline_code=sub["decline_code"],
                proposed_action=policy.allowed_action,  # the known-correct action - see module docstring
                amount_paise=sub["amount_paise"],
            )

            tool_result = None
            tool_name = None
            if decision.execute and decision.final_action in ACTION_TO_TOOL:
                tool_name = ACTION_TO_TOOL[decision.final_action]
                if tool_name == "create_payment_link":
                    args = {
                        "subscription_id": sub["subscription_id"],
                        "amount_paise": sub["amount_paise"],
                        "description": f"Complete your payment for {sub['plan']}",
                    }
                else:
                    args = {"subscription_id": sub["subscription_id"], "amount_paise": sub["amount_paise"]}
                call = await client.call_tool(tool_name, args)
                tool_result = call.content[0].text if call.content else None

            entry = {
                "subscription_id": sub["subscription_id"],
                "decline_code": sub["decline_code"],
                "amount_paise": sub["amount_paise"],
                "final_action": decision.final_action.value,
                "tool": tool_name,
                "tool_result": tool_result,
            }
            _log(log_entries, **entry)
            print(f"{sub['subscription_id']}  {sub['decline_code']:24s} -> {tool_name or '(no tool)'}")
            if tool_result:
                parsed = json.loads(tool_result)
                real_id = parsed.get("id", "?")
                print(f"    real Razorpay id: {real_id}  simulated={parsed.get('simulated')}")

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("w", encoding="utf-8") as f:
        for e in log_entries:
            f.write(json.dumps(e, default=str) + "\n")

    lines = [
        "# REAL_MCP_RESULTS",
        "",
        "Output of `real_mcp_demo.py` - real calls to Razorpay's test-mode",
        "API through their own official MCP server (`razorpay/razorpay-mcp-server`",
        "via Docker), using real `rzp_test_` keys. Unlike every other run in this",
        "repo, nothing here is simulated: every ID below is a real object created",
        "in a real (test-mode) Razorpay account.",
        "",
        "| Subscription | Decline code | Action | Tool | Real Razorpay ID |",
        "|---|---|---|---|---|",
    ]
    for e in log_entries:
        real_id = "-"
        if e["tool_result"]:
            real_id = json.loads(e["tool_result"]).get("id", "?")
        lines.append(
            f"| {e['subscription_id']} | {e['decline_code']} | {e['final_action']} | "
            f"{e['tool'] or '-'} | `{real_id}` |"
        )
    RESULTS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {LOG_PATH} and {RESULTS_PATH}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    asyncio.run(run(n))
