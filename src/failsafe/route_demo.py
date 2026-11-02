"""
Stretch goal: Route (Razorpay's split-settlement / Linked Account feature)
- two-sided commerce, not just single-party checkout.

Kept as a separate, standalone script rather than woven into the main
150-record recovery pipeline (agent.py) - Route is genuinely a different
scenario (a referral partner earning a share of a recovered payment) and
mixing it into the already-verified main pipeline would risk destabilizing
results that are already real and tested. This runs independently, through
the same MCP server / audit-log pattern as the main pipeline, plus the same
spending-cap *value* gate.py defines (see the comment in run() below for
exactly what "same" does and doesn't mean here - this file does not call
Gate.evaluate()).

Scenario: some recovered subscriptions came through a referral partner
(e.g. an affiliate who brought that customer to the merchant). When one of
those recovers, the partner earns a fixed percentage via a Route transfer,
split at the same moment the recovery order is created - not a separate
manual payout step.

Safety: the demo runs in SIMULATE mode unless `--live` is passed
explicitly, even when real Razorpay test keys are configured, and it prints
which audit log it appends to. `--audit-log PATH` sends its events somewhere
other than the default `logs/audit_log.jsonl` (the flagship log behind the
published results); `--results PATH` does the same for ROUTE_RESULTS.md.

    python -m failsafe.route_demo                                  # simulate
    python -m failsafe.route_demo --audit-log logs/route_audit.jsonl
    python -m failsafe.route_demo --live                           # real test-mode Route orders
"""

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mcp import Client

from failsafe import razorpay_client
from failsafe.audit_log import AuditLogger
from failsafe.gate import MAX_ACTION_AMOUNT_PAISE
from failsafe.mcp_server import PARTNER_LINKED_ACCOUNT_ID, simulate_mode
from failsafe.mcp_server import server as mcp_server
from failsafe.paths import LOGS_DIR, RESULTS_DIR

AUDIT_PATH = LOGS_DIR / "audit_log.jsonl"
RESULTS_PATH = RESULTS_DIR / "ROUTE_RESULTS.md"

PARTNER_SHARE_PCT = 0.05  # referral partner earns 5% of the recovered amount

# Small standalone synthetic scenario: 5 previously-halted subscriptions
# that are now being recovered, each attributed to a referral partner.
REFERRED_RECOVERIES: list[dict[str, Any]] = [
    {
        "subscription_id": "sub_route_demo_001",
        "amount_paise": 149900,
        "referrer": "partner_affiliate_01",
    },
    {
        "subscription_id": "sub_route_demo_002",
        "amount_paise": 899900,
        "referrer": "partner_affiliate_02",
    },
    {
        "subscription_id": "sub_route_demo_003",
        "amount_paise": 29900,
        "referrer": "partner_affiliate_01",
    },
    {
        "subscription_id": "sub_route_demo_004",
        "amount_paise": 55000000,
        "referrer": "partner_affiliate_03",
    },  # deliberately over the gate's cap
    {
        "subscription_id": "sub_route_demo_005",
        "amount_paise": 249900,
        "referrer": "partner_affiliate_02",
    },
]


async def run(audit_path: Path | None = None, results_path: Path | None = None) -> None:
    audit = AuditLogger(audit_path or AUDIT_PATH)
    audit.log("route_demo_started", total=len(REFERRED_RECOVERIES))

    results = []
    async with Client(mcp_server) as client:
        for rec in REFERRED_RECOVERIES:
            partner_share = int(rec["amount_paise"] * PARTNER_SHARE_PCT)

            # Route transfers are still a money action - still gated, but not
            # through Gate.evaluate(): a Route transfer has no decline_code,
            # so the policy-lookup and idempotency parts of the gate don't
            # apply here (an earlier version of this file constructed a Gate
            # instance for this check and never used it - removed; this
            # reuses only the cap constant, not the class). The oversized
            # transfer below (sub_route_demo_004) is still genuinely blocked
            # twice over: here, and independently by mcp_server.py's
            # _enforce_tool_level_cap() inside initiate_route_transfer itself
            # - see tests/test_mcp_server_guard.py for that second layer.
            if rec["amount_paise"] > MAX_ACTION_AMOUNT_PAISE:
                audit.log(
                    "route_transfer_blocked",
                    subscription_id=rec["subscription_id"],
                    reason=f"Amount {rec['amount_paise'] / 100:.2f} exceeds per-action cap "
                    f"of {MAX_ACTION_AMOUNT_PAISE / 100:.2f}.",
                )
                results.append({**rec, "partner_share_paise": partner_share, "blocked": True})
                print(f"{rec['subscription_id']}  BLOCKED (exceeds spending cap)")
                continue

            call = await client.call_tool(
                "initiate_route_transfer",
                {
                    "subscription_id": rec["subscription_id"],
                    "amount_paise": rec["amount_paise"],
                    "partner_share_paise": partner_share,
                },
            )
            tool_result = getattr(call.content[0], "text", None) if call.content else None
            audit.log(
                "route_transfer",
                subscription_id=rec["subscription_id"],
                referrer=rec["referrer"],
                amount_paise=rec["amount_paise"],
                partner_share_paise=partner_share,
                partner_linked_account_id=PARTNER_LINKED_ACCOUNT_ID,
                result=tool_result,
            )
            results.append(
                {
                    **rec,
                    "partner_share_paise": partner_share,
                    "blocked": False,
                    "tool_result": tool_result,
                }
            )
            print(
                f"{rec['subscription_id']}  merchant gets Rs {(rec['amount_paise'] - partner_share) / 100:.2f}, "
                f"{rec['referrer']} gets Rs {partner_share / 100:.2f}"
            )

    audit.log("route_demo_finished", total=len(results))
    write_results(results, results_path)


def write_results(results: list[dict], results_path: Path | None = None):
    executed = [r for r in results if not r["blocked"]]
    blocked = [r for r in results if r["blocked"]]
    total_partner_paise = sum(r["partner_share_paise"] for r in executed)

    lines = [
        "# ROUTE_RESULTS (stretch goal)",
        "",
        "Two-sided split settlement via Razorpay Route. A referral partner earns "
        f"{PARTNER_SHARE_PCT * 100:.0f}% of each recovered subscription attributed to them, "
        "split at order-creation time via a Route transfer - not a manual payout step.",
        "",
        f"Linked Account used: `{PARTNER_LINKED_ACCOUNT_ID}` "
        f"({'a real onboarded account' if not PARTNER_LINKED_ACCOUNT_ID.startswith('acc_sim_') else 'simulated - no real Linked Account was onboarded for this build, see BUILD_LOG.md §7.4'}).",
        "",
        f"- Recoveries processed: **{len(results)}**",
        f"- Route transfers executed: **{len(executed)}**",
        f"- Blocked by the spending-cap gate: **{len(blocked)}** "
        f"(demonstrates the cap applies here too, not just in the main pipeline)",
        f"- Total partner payout across executed transfers: **Rs {total_partner_paise / 100:,.2f}**",
        "",
        "## Per transaction",
        "",
        "| Subscription | Referrer | Amount | Partner share | Status |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        status = "BLOCKED (cap)" if r["blocked"] else "executed"
        lines.append(
            f"| {r['subscription_id']} | {r['referrer']} | Rs {r['amount_paise'] / 100:,.2f} | "
            f"Rs {r['partner_share_paise'] / 100:,.2f} | {status} |"
        )

    out = results_path or RESULTS_PATH
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {out}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m failsafe.route_demo",
        description="Route split-settlement demo. Runs in SIMULATE mode unless --live is given.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--simulate",
        dest="live",
        action="store_false",
        help="force simulate mode, even if Razorpay test keys are configured (default)",
    )
    mode.add_argument(
        "--live",
        dest="live",
        action="store_true",
        help="use the configured rzp_test_ keys and place real test-mode Route orders",
    )
    parser.set_defaults(live=False)
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=None,
        metavar="PATH",
        help="audit log to append events to (default: logs/audit_log.jsonl, the flagship log)",
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=None,
        metavar="PATH",
        help="where to write the results markdown (default: ROUTE_RESULTS.md in the repo root)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    audit_path: Path = args.audit_log or AUDIT_PATH
    results_path: Path = args.results or RESULTS_PATH

    if args.live and not razorpay_client.keys_configured():
        print(
            "error: --live needs Razorpay test keys (RAZORPAY_KEY_ID starting with rzp_test_ "
            "and RAZORPAY_KEY_SECRET) in the environment or .env, and none are configured. "
            "Run without --live to use simulate mode.",
            file=sys.stderr,
        )
        return 2
    if audit_path.is_dir():
        print(
            f"error: --audit-log must be a file path, not a directory: {audit_path}",
            file=sys.stderr,
        )
        return 2
    if results_path.is_dir():
        print(
            f"error: --results must be a file path, not a directory: {results_path}",
            file=sys.stderr,
        )
        return 2

    if args.live:
        print("Mode: LIVE (real Razorpay test-mode Route orders)")
    else:
        print("Mode: SIMULATE (no Razorpay calls)")
    print(f"Audit log: {audit_path.resolve()} (appending)")
    if audit_path.resolve() == AUDIT_PATH.resolve():
        print(
            "Note: this is the flagship audit log behind the published results. "
            "Pass --audit-log PATH to keep Route events out of it."
        )

    ctx = contextlib.nullcontext() if args.live else simulate_mode(True)
    with ctx:
        asyncio.run(run(audit_path, results_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
