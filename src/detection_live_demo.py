"""
Live demonstration of the new revenue-at-risk DETECTION stage (detect.py),
run against the REAL local Ollama server (llama3.1:8b) - not mocked, not a
historical log line replayed. Mirrors diagnosis_live_demo.py's structure
and honesty conventions exactly, one stage earlier in the pipeline.

Proves, end to end, through the actual pipeline code path
(recovery_pipeline.process_record, the same function agent.py's
process_one() calls):

  1. Detection genuinely happens: a MIXED pool of synthetic subscriptions
     (some genuinely healthy, some genuinely at-risk -
     generate_detection_pool.py) is classified using ONLY four raw
     signals (previous_retry_count, days_since_last_successful_charge,
     most_recent_gateway_response, subscription_status) - never a
     precomputed "is_at_risk" boolean.
  2. Detection is genuinely fallible, and a wrong call has a REAL
     consequence, not a cosmetic one: a record classified "leave_alone"
     never reaches diagnosis, the action proposal, or the gate at all - if
     it was actually at-risk, nothing is ever attempted for it. A record
     classified "needs_recovery_attention" genuinely proceeds into
     diagnosis/the gate - if it was actually healthy, that's wasted real
     pipeline work (typically a real flag_for_manual_review call on a fine
     customer, since a healthy record has no decline_code for the policy
     table to match).

Kept as a separate, standalone script rather than folded into agent.py's
150-record flagship run or diagnosis_live_demo.py's 30-record diagnosis
run - same reasoning both of those already documented: this is now
potentially THREE live Ollama calls per record (detect, then diagnose,
then propose, for anything classified as needing attention), so re-running
either existing flagship number with detection added was not attempted in
this session. See BUILD_LOG.md's dated entry and README.md §6 for the
honest disclosure of exactly what this subset does and does not prove.

Writes to its OWN audit log and results file
(logs/detection_demo_audit.jsonl, DETECTION_DEMO_RESULTS.md) rather than
touching logs/audit_log.jsonl, RESULTS.md, logs/diagnosis_demo_audit.jsonl,
or DIAGNOSIS_DEMO_RESULTS.md - none of those already-verified runs are
touched by running this script, matching this project's existing
convention (logs/pre_escalation_rules/, logs/pre_accuracy_fix/, and
diagnosis_live_demo.py's own separate-file choice all preserve prior runs
rather than mutate or overwrite them).

SIMULATE is forced True here, patching BOTH `mcp_server.SIMULATE` and
`mcp_server._rp.simulate` - diagnosis_live_demo.py's docstring documents in
detail why patching only the first is insufficient when real `rzp_test_`
keys are present in `.env` (mcp_server._rp is constructed once at import
time and branches on its OWN `self.simulate`, not the module-level name).

Usage: python detection_live_demo.py [N]   (default: however many records
generate_detection_pool.py wrote, currently 30)
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

from mcp import Client

import mcp_server
from audit_log import AuditLogger
from decline_codes import RecoveryAction
from gate import Gate
from mcp_server import server as mcp_server_instance
from ollama_client import DEFAULT_SITUATION
from recovery_pipeline import LEFT_ALONE_BY_DETECTION, process_record

DATA_PATH = Path(__file__).parent.parent / "data" / "detection_pool.json"
AUDIT_PATH = Path(__file__).parent.parent / "logs" / "detection_demo_audit.jsonl"
RESULTS_PATH = Path(__file__).parent.parent / "DETECTION_DEMO_RESULTS.md"

ID_FIELD = "subscription_id"

ACTION_TO_TOOL = {
    RecoveryAction.IMMEDIATE_RETRY: "create_retry_order",
    RecoveryAction.DELAYED_RETRY: "create_retry_order",
    RecoveryAction.PAYMENT_LINK_NUDGE: "create_payment_link",
    RecoveryAction.NO_ACTION_FRAUD: "flag_for_manual_review",
    RecoveryAction.NO_ACTION_UNRECOVERABLE: "flag_for_manual_review",
}


async def run(n: int | None) -> list[dict]:
    records = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    if n is not None:
        records = records[:n]

    if AUDIT_PATH.exists():
        AUDIT_PATH.unlink()
    audit = AuditLogger(AUDIT_PATH)
    audit.log("detection_demo_started", total_records=len(records))

    gate = Gate()
    results = []
    with patch.object(mcp_server, "SIMULATE", True), \
         patch.object(mcp_server._rp, "simulate", True):
        async with Client(mcp_server_instance) as client:
            for i, sub in enumerate(records):
                result = await process_record(
                    client, gate, audit, sub,
                    id_field=ID_FIELD,
                    action_to_tool=ACTION_TO_TOOL,
                    item_label_field="plan",
                    situation=DEFAULT_SITUATION,
                    record_label="Subscription",
                    raw_signal_field="raw_decline_message",
                    run_detection=True,
                )
                result["ground_truth_needs_attention"] = sub["ground_truth_needs_attention"]
                results.append(result)
                truth = "AT-RISK" if sub["ground_truth_needs_attention"] else "HEALTHY"
                match = "MATCH" if result["detection_matched_ground_truth"] else "MISS "
                print(
                    f"[{i+1}/{len(records)}] {sub['subscription_id']}  truth={truth:8s} "
                    f"detected={str(result['detection_classification']):26s} [{match}] "
                    f"-> final_action={result['final_action']}"
                )

    audit.log("detection_demo_finished", total_processed=len(results))
    write_results(results)
    return results


def write_results(results: list[dict]):
    total = len(results)
    matched = sum(1 for r in results if r["detection_matched_ground_truth"])
    mismatched = total - matched
    failed = sum(1 for r in results if r["detection_classification"] is None)

    false_positives = [
        r for r in results
        if not r["ground_truth_needs_attention"] and r["detection_classification"] == "needs_recovery_attention"
    ]
    false_negatives = [
        r for r in results
        if r["ground_truth_needs_attention"] and r["detection_classification"] == "leave_alone"
    ]
    true_positives = [
        r for r in results
        if r["ground_truth_needs_attention"] and r["detection_classification"] == "needs_recovery_attention"
    ]
    true_negatives = [
        r for r in results
        if not r["ground_truth_needs_attention"] and r["detection_classification"] == "leave_alone"
    ]

    # The "real downstream consequence" measurement, not just an abstract
    # accuracy number: how many false negatives left real (simulated)
    # revenue completely unattempted, and how many false positives
    # triggered a real, wasted MCP tool call on a healthy customer.
    unattempted_at_risk_value_paise = sum(r["amount_paise"] for r in false_negatives if r.get("amount_paise"))
    wasted_calls_on_healthy = sum(1 for r in false_positives if r["final_action"] != LEFT_ALONE_BY_DETECTION)

    lines = [
        "# DETECTION_DEMO_RESULTS",
        "",
        "Live run of the new revenue-at-risk DETECTION stage (detect.py) against",
        "the real local Ollama server (llama3.1:8b, temperature 0) - not mocked.",
        "",
        f"Records processed: **{total}** - a mixed pool of synthetic subscriptions "
        "(generate_detection_pool.py): some genuinely healthy (no decline_code, "
        "no retries, a recent successful charge), some genuinely at-risk (real "
        "retries, a stale gap since the last successful charge, a real decline "
        "code assigned as ground truth ONLY for scoring - never given to detect.py). "
        "This does NOT touch the 150-record flagship batch or the 30-record "
        "diagnosis demo - see BUILD_LOG.md and README.md §6 for what this subset "
        "does and does not prove.",
        "",
        f"- **Detection accuracy (classification matches ground truth): "
        f"{matched}/{total} ({matched/total*100:.1f}%)**",
        f"- Detection failures (no usable tool call): **{failed}**",
        f"- **False positives (healthy, wrongly flagged 'needs_recovery_attention'): "
        f"{len(false_positives)}** - of which **{wasted_calls_on_healthy}** genuinely "
        "proceeded into the pipeline and triggered a real, wasted MCP tool call "
        "(typically flag_for_manual_review) on a customer who needed no such thing.",
        f"- **False negatives (at-risk, wrongly cleared 'leave_alone'): "
        f"{len(false_negatives)}** - representing **Rs "
        f"{unattempted_at_risk_value_paise/100:,.2f}** of genuinely at-risk "
        "subscription value that was never diagnosed, gated, or attempted at all "
        "as a direct result of the wrong detection call.",
        f"- True positives: **{len(true_positives)}**, true negatives: **{len(true_negatives)}**",
        "",
        "## Per-record detail",
        "",
        "| Subscription | Ground truth | Detected | Match | Final action |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        truth = "at_risk" if r["ground_truth_needs_attention"] else "healthy"
        lines.append(
            f"| {r['subscription_id']} | {truth} | {r['detection_classification']} | "
            f"{'yes' if r['detection_matched_ground_truth'] else 'no'} | {r['final_action']} |"
        )

    RESULTS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {RESULTS_PATH}")
    print(f"\nDetection accuracy: {matched}/{total} ({matched/total*100:.1f}%)")
    print(f"False positives: {len(false_positives)} (wasted real tool calls: {wasted_calls_on_healthy})")
    print(f"False negatives: {len(false_negatives)} (Rs {unattempted_at_risk_value_paise/100:,.2f} never attempted)")


if __name__ == "__main__":
    n_arg = int(sys.argv[1]) if len(sys.argv) > 1 else None
    asyncio.run(run(n_arg))
