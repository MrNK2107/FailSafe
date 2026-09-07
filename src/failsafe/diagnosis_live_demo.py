"""
Live demonstration of the new root-cause diagnosis stage (diagnose.py),
run against the REAL local Ollama server (llama3.1:8b) - not mocked, not
a historical log line replayed. Proves two things end to end through the
actual pipeline code path (recovery_pipeline.process_record, the same
function agent.py's process_one() calls):

  1. Diagnosis genuinely happens: decline_code is inferred from ONLY the
     raw, ambiguous bank/gateway message (generate_data.py's
     `raw_decline_message`), never handed the ground truth.
  2. Diagnosis is genuinely fallible, and a wrong diagnosis has REAL
     downstream consequences: the gate evaluates the DIAGNOSED code, so a
     misdiagnosis can change final_action away from what ground truth's
     own policy would have produced. This script reports both the
     diagnosis accuracy AND how many records had their final_action
     changed by a misdiagnosis - not just an abstract accuracy number.

Kept as a separate, standalone script rather than folded into agent.py's
150-record flagship run - same reasoning as route_demo.py's own docstring:
running this live through Ollama is slow (~20-30s per diagnosis call, and
another ~20-30s for the existing action-proposal call - two real model
calls per record now, not one), so re-running the full 150-record
flagship batch with two live LLM calls per record was not attempted in
this session - see BUILD_LOG.md's dated entry and README.md §6 for the
honest disclosure of exactly what subset this covers and what it doesn't.
This script instead runs a smaller, deterministic slice (the first N
records of the already-committed, seeded 150-record dataset - not
cherry-picked) far enough to produce a real, non-trivial accuracy number.

Writes to its OWN audit log and results file
(logs/diagnosis_demo_audit.jsonl, DIAGNOSIS_DEMO_RESULTS.md) rather than
touching logs/audit_log.jsonl or RESULTS.md - the already-verified
flagship 150-record run and its RESULTS.md are left completely untouched
by running this script, matching this project's existing convention of
never silently overwriting a previously-verified run
(logs/pre_escalation_rules/, logs/pre_accuracy_fix/, etc. all preserve
prior runs rather than mutate them in place; this script goes one step
further and simply never writes to that path at all).

SIMULATE is forced True here - this script exists to measure diagnosis/
gate behavior, not to create real Razorpay test-mode objects, so no real
API call should be a side effect of running it. Real MCP tool calls
against real test-mode keys already exist elsewhere in this project
(REAL_MCP_RESULTS.md); duplicating that here would add cost/objects with
no bearing on what this script measures.

Historical note, kept as a warning: forcing simulate used to require
patching TWO things (`mcp_server.SIMULATE` plus `mcp_server._rp.simulate`)
because _rp's simulate state was a snapshot bound at construction - patch
only the first and _rp still made a REAL API call, which once burned a
real "test mode limit of 30 reached for payment_link" ServerError. The
WP3 import-hygiene overhaul removed the shadow copies entirely: simulate
state now lives in ONE dynamic knob (failsafe.razorpay_client.simulate_mode)
and callers force it with force_simulate(). The failure mode above is
structurally impossible now - there is no second copy left to drift.

Usage: python diagnosis_live_demo.py [N]   (default N=30)
"""

import asyncio
import json
import sys

from mcp import Client

from failsafe.audit_log import AuditLogger
from failsafe.decline_codes import RecoveryAction, get_decline_code
from failsafe.gate import Gate
from failsafe.mcp_server import server as mcp_server_instance
from failsafe.ollama_client import DEFAULT_SITUATION
from failsafe.paths import DATA_DIR, LOGS_DIR, PROJECT_ROOT
from failsafe.razorpay_client import force_simulate
from failsafe.recovery_pipeline import process_record

DATA_PATH = DATA_DIR / "halted_subscriptions.json"
AUDIT_PATH = LOGS_DIR / "diagnosis_demo_audit.jsonl"
RESULTS_PATH = PROJECT_ROOT / "DIAGNOSIS_DEMO_RESULTS.md"

ID_FIELD = "subscription_id"

ACTION_TO_TOOL = {
    RecoveryAction.IMMEDIATE_RETRY: "create_retry_order",
    RecoveryAction.DELAYED_RETRY: "create_retry_order",
    RecoveryAction.PAYMENT_LINK_NUDGE: "create_payment_link",
    RecoveryAction.NO_ACTION_FRAUD: "flag_for_manual_review",
    RecoveryAction.NO_ACTION_UNRECOVERABLE: "flag_for_manual_review",
}


async def run(n: int) -> list[dict]:
    records = json.loads(DATA_PATH.read_text(encoding="utf-8"))[:n]

    # Fresh audit log every run of this demo script - it's a diagnostic
    # tool re-run during development, not an append-only production trail
    # like logs/audit_log.jsonl.
    if AUDIT_PATH.exists():
        AUDIT_PATH.unlink()
    audit = AuditLogger(AUDIT_PATH)
    audit.log("diagnosis_demo_started", total_records=len(records), model_calls_per_record=2)

    gate = Gate()
    results = []
    with force_simulate():
        async with Client(mcp_server_instance) as client:
            for i, sub in enumerate(records):
                result = await process_record(
                    client,
                    gate,
                    audit,
                    sub,
                    id_field=ID_FIELD,
                    action_to_tool=ACTION_TO_TOOL,
                    item_label_field="plan",
                    situation=DEFAULT_SITUATION,
                    record_label="Subscription",
                    raw_signal_field="raw_decline_message",
                )
                result["raw_decline_message"] = sub["raw_decline_message"]
                results.append(result)
                match = "MATCH" if result["diagnosis_matched_ground_truth"] else "MISS "
                print(
                    f"[{i + 1}/{len(records)}] {sub['subscription_id']}  "
                    f"true={result['decline_code']:32s} diagnosed={str(result['diagnosed_decline_code']):32s} "
                    f"[{match}] -> final_action={result['final_action']}"
                )

    audit.log("diagnosis_demo_finished", total_processed=len(results))
    write_results(results)
    return results


def write_results(results: list[dict]):
    total = len(results)
    matched = sum(1 for r in results if r["diagnosis_matched_ground_truth"])
    mismatched = total - matched
    failed = sum(1 for r in results if r["diagnosed_decline_code"] is None)

    # The "real downstream consequences" measurement: for every record,
    # what final_action would ground truth's OWN policy have produced,
    # compared to what actually happened using the diagnosed code. A
    # misdiagnosis that still happens to share the same allowed_action as
    # the true code (e.g. two customer-fixable codes that both resolve to
    # payment_link_nudge) has no visible consequence; this counts only the
    # cases where the final action genuinely differs.
    changed_action = 0
    for r in results:
        true_policy_action = get_decline_code(r["decline_code"]).allowed_action.value
        if not r["diagnosis_matched_ground_truth"] and r["final_action"] != true_policy_action:
            changed_action += 1

    lines = [
        "# DIAGNOSIS_DEMO_RESULTS",
        "",
        "Live run of the new root-cause diagnosis stage (diagnose.py) against",
        "the real local Ollama server (llama3.1:8b, temperature 0) - not mocked.",
        "",
        f"Records processed: **{total}** (the first {total} of the seeded, "
        "already-committed 150-record `data/halted_subscriptions.json` - "
        "a deterministic slice, not cherry-picked). This does NOT re-run the "
        "full 150-record flagship batch - see BUILD_LOG.md and README.md §6 "
        "for why, and for exactly what this subset does and does not prove.",
        "",
        f"- **Diagnosis accuracy (diagnosed_decline_code == true decline_code): "
        f"{matched}/{total} ({matched / total * 100:.1f}%)**",
        f"- Misdiagnoses: **{mismatched}/{total}**, of which diagnosis failures "
        f"(no usable tool call / unrecognized code returned): **{failed}**",
        f"- Misdiagnoses that changed the final recovery action versus what ground "
        f"truth's own policy would have given: **{changed_action}/{total}** - the "
        "concrete proof that a wrong diagnosis has real downstream consequences "
        "here, not just a logged-and-ignored accuracy statistic.",
        "",
        "## Per-record detail",
        "",
        "| Subscription | Raw message | True code | Diagnosed code | Match | Final action |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        raw = r.get("raw_decline_message", "")
        lines.append(
            f"| {r['subscription_id']} | {raw} | {r['decline_code']} | "
            f"{r['diagnosed_decline_code']} | {'yes' if r['diagnosis_matched_ground_truth'] else 'no'} | "
            f"{r['final_action']} |"
        )

    RESULTS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {RESULTS_PATH}")
    print(f"\nDiagnosis accuracy: {matched}/{total} ({matched / total * 100:.1f}%)")
    print(f"Final-action changed by misdiagnosis: {changed_action}/{total}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    asyncio.run(run(n))
