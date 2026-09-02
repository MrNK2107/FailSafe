"""
The orchestrator. For each halted subscription:
  1. DIAGNOSE a decline_code from the raw, ambiguous bank/gateway message
     (diagnose.py) - never handed the ground-truth decline_code. See
     BUILD_LOG.md §14 for why this stage exists and what it replaced.
  2. Ask the local Ollama model to PROPOSE an action for the DIAGNOSED
     code (ollama_client).
  3. Send that proposal through the GATE - deterministic, never trusts the
     model (gate.py). The gate evaluates the DIAGNOSED code too, so a
     wrong diagnosis can change the final action. The gate can also
     override the model entirely regardless of diagnosis.
  4. If the gate allows a real action, execute it through the MCP SERVER
     (mcp_server.py) via a real in-process MCP Client/Server round-trip.
  5. Log every step to the audit trail (audit_log.py), whether allowed or
     denied - including both the diagnosed code and the ground-truth code,
     so diagnosis accuracy can be measured honestly.

The actual per-record diagnose/propose/gate/execute/audit sequence lives in
recovery_pipeline.py, shared with agent_onetime.py - this file supplies the
subscription-specific configuration (field names, situation text) plus
everything agent_onetime.py deliberately doesn't have: checkpoint/resume,
the --inject-failure CLI, and (so far) the diagnosis stage itself - see
recovery_pipeline.py's docstring for why agent_onetime.py doesn't diagnose.
See recovery_pipeline.py's docstring and BUILD_LOG.md §12/§14 for why this
was extracted.

Note on transport: the MCP Client below is constructed directly against the
in-process `server` object, which is a supported mode of the official SDK
(mcp.Client accepts an MCPServer instance) and exercises the real MCP
protocol messages. Swapping to a separate OS process is a one-line change
(pass StdioServerParameters pointing at `python mcp_server.py` instead) -
kept in-process here to remove subprocess-management risk from a short
build window; call this out explicitly in the pitch video.
"""

import argparse
import asyncio
import json
from pathlib import Path

from mcp import Client

from audit_log import AuditLogger
from decline_codes import RecoveryAction
from gate import Gate
from mcp_server import server as mcp_server
from ollama_client import DEFAULT_SITUATION
from recovery_pipeline import count_prior_attempts, process_record, render_decline_code_table

DATA_PATH = Path(__file__).parent.parent / "data" / "halted_subscriptions.json"
AUDIT_PATH = Path(__file__).parent.parent / "logs" / "audit_log.jsonl"
CHECKPOINT_PATH = Path(__file__).parent.parent / "logs" / "results_checkpoint.jsonl"
RESULTS_PATH = Path(__file__).parent.parent / "RESULTS.md"

ID_FIELD = "subscription_id"

ACTION_TO_TOOL = {
    RecoveryAction.IMMEDIATE_RETRY: "create_retry_order",
    RecoveryAction.DELAYED_RETRY: "create_retry_order",
    RecoveryAction.PAYMENT_LINK_NUDGE: "create_payment_link",
    RecoveryAction.NO_ACTION_FRAUD: "flag_for_manual_review",
    RecoveryAction.NO_ACTION_UNRECOVERABLE: "flag_for_manual_review",
}


def _count_prior_attempts(audit_events: list[dict], subscription_id: str) -> int:
    """Thin, subscription-specific wrapper over recovery_pipeline.count_prior_attempts -
    kept as a top-level name here since tests import it as `agent._count_prior_attempts`."""
    return count_prior_attempts(audit_events, ID_FIELD, subscription_id)


async def process_one(
    client: Client,
    gate: Gate,
    audit: AuditLogger,
    sub: dict,
    inject_failure: str | None = None,
    prior_attempt_count: int = 0,
) -> dict:
    return await process_record(
        client, gate, audit, sub,
        id_field=ID_FIELD,
        action_to_tool=ACTION_TO_TOOL,
        item_label_field="plan",
        situation=DEFAULT_SITUATION,
        record_label="Subscription",
        inject_failure=inject_failure,
        prior_attempt_count=prior_attempt_count,
        # generate_data.py now attaches a raw, ambiguous bank/gateway
        # decline message per record - this is what makes root-cause
        # diagnosis (diagnose.py) actually run for this pipeline instead
        # of being skipped. See recovery_pipeline.py's docstring.
        raw_signal_field="raw_decline_message",
    )


def _load_checkpoint() -> list[dict]:
    if not CHECKPOINT_PATH.exists():
        return []
    with CHECKPOINT_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _append_checkpoint(result: dict):
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CHECKPOINT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, default=str) + "\n")


def parse_args():
    parser = argparse.ArgumentParser(description="Run the subscription recovery agent pipeline.")
    parser.add_argument(
        "--inject-failure",
        choices=[
            "llm_parse_failure",
            "llm_invalid_action",
            "unknown_decline_code",
            "repeat_attempts",
            "diagnosis_parse_failure",
        ],
        default=None,
        help=(
            "Force the FIRST remaining record down a real graceful-degradation "
            "path (D4, BUILD_LOG.md §7.3) instead of calling Ollama for it - "
            "for demoing 'one failure handled gracefully' live on camera "
            "instead of pointing at a historical log line. "
            "'unknown_decline_code' skips the LLM/gate entirely and flags the "
            "record for manual review, simulating a decline code with no "
            "policy entry at all. 'repeat_attempts' calls the real model "
            "normally but forces the gate's cross-run attempt-cap stopping "
            "rule to fire (gate.py MAX_ATTEMPTS_PER_SUBSCRIPTION), demoing "
            "compliant escalation live. 'diagnosis_parse_failure' forces the "
            "new root-cause diagnosis stage (diagnose.py) to return no usable "
            "tool call, demoing that diagnosis failing gracefully falls back "
            "to manual review instead of guessing a decline_code - the "
            "action-proposal LLM call is never reached for this record. "
            "Every other record in the run is unaffected and calls the real "
            "model normally."
        ),
    )
    return parser.parse_args()


async def run(inject_failure: str | None = None):
    if not DATA_PATH.exists():
        raise SystemExit("No data found. Run `python src/generate_data.py` first.")

    subscriptions = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    gate = Gate()
    audit = AuditLogger(AUDIT_PATH)

    # Resumable/kill-safe: a batch this size (150 x ~5-20s Ollama calls) can
    # take tens of minutes, and a run getting interrupted partway through
    # (it happened - a background run was killed after 7 records) shouldn't
    # throw away real work. Anything already in the checkpoint is skipped.
    results = _load_checkpoint()
    done_ids = {r["subscription_id"] for r in results}
    remaining = [s for s in subscriptions if s["subscription_id"] not in done_ids]

    acted_actions = {"immediate_retry", "delayed_retry", "payment_link_nudge"}
    already_spent = sum(
        r["amount_paise"] for r in results
        if r["final_action"] in acted_actions and r["gate_executed"]
    )
    already_seen_keys = {
        (r["subscription_id"], r["final_action"]) for r in results if r["gate_executed"]
    }
    gate.seed_from_checkpoint(already_spent, already_seen_keys)

    # Cross-run history for the attempt-cap stopping rule (gate.py
    # MAX_ATTEMPTS_PER_SUBSCRIPTION): read once, up front, before this run
    # appends anything new - reflects every attempt any PRIOR run already
    # made, not just this run's in-memory state.
    audit_history = audit.read_all()

    audit.log(
        "run_started",
        total_subscriptions=len(subscriptions),
        already_done_from_checkpoint=len(done_ids),
        remaining=len(remaining),
    )
    print(f"{len(done_ids)} already done (checkpoint), {len(remaining)} remaining")

    async with Client(mcp_server) as client:
        for i, sub in enumerate(remaining):
            # Only the first record in a run can be injected - keeps the
            # rest of the batch's numbers meaningful while still proving
            # the failure path live.
            inject = inject_failure if (inject_failure and i == 0) else None
            if inject:
                print(f"[demo] injecting '{inject}' failure for {sub['subscription_id']}")
            try:
                prior_attempts = _count_prior_attempts(audit_history, sub["subscription_id"])
                result = await process_one(
                    client, gate, audit, sub, inject_failure=inject, prior_attempt_count=prior_attempts
                )
            except Exception as e:
                # A single record's unexpected failure must not take down a
                # 150-record batch run - log it and keep going. Discovered
                # the hard way: an unhandled Ollama 500 crashed the entire
                # first full run. See BUILD_LOG.md.
                audit.log(
                    "record_processing_error",
                    subscription_id=sub["subscription_id"],
                    error=str(e),
                )
                result = {
                    "subscription_id": sub["subscription_id"],
                    "amount_paise": sub["amount_paise"],
                    "decline_code": sub["decline_code"],
                    "diagnosed_decline_code": None,
                    "diagnosis_matched_ground_truth": None,
                    "final_action": "no_action_unrecoverable",
                    "gate_executed": False,
                    "llm_matched_policy": False,
                    "simulated_customer_response": False,
                    "tool_result": None,
                }
            results.append(result)
            _append_checkpoint(result)
            print(f"{sub['subscription_id']}  {sub['decline_code']:32s} -> {result['final_action']}")

    audit.log("run_finished", total_processed=len(results))
    write_results(results)


def write_results(results: list[dict]):
    if not results:
        RESULTS_PATH.write_text("# RESULTS\n\nNo records processed.\n", encoding="utf-8")
        print(f"\nWrote {RESULTS_PATH} (empty run)")
        return

    acted_actions = {"immediate_retry", "delayed_retry", "payment_link_nudge"}

    total_halted_paise = sum(r["amount_paise"] for r in results)
    acted_on = [r for r in results if r["final_action"] in acted_actions and r["gate_executed"]]
    recovered = [r for r in acted_on if r["simulated_customer_response"]]
    recovered_paise = sum(r["amount_paise"] for r in recovered)

    hard_blocked = [r for r in results if not r["gate_executed"]]
    overridden = [r for r in results if not r["llm_matched_policy"]]
    fraud_refused = [r for r in results if r["final_action"] == "no_action_fraud"]
    unrecoverable = [r for r in results if r["final_action"] == "no_action_unrecoverable"]

    lines = [
        "# RESULTS",
        "",
        "Generated from a run against synthetic, schema-accurate-but-fabricated",
        "data. `simulated_customer_response` is a labeled synthetic assumption",
        "(see decline_codes.py `simulated_success_rate`), not a real payment",
        "outcome - test mode cannot produce a real customer completing a charge.",
        "This file reports it honestly as simulated throughout.",
        "",
        f"- Total halted subscriptions processed: **{len(results)}**",
        f"- Total value of halted subscriptions: **Rs {total_halted_paise/100:,.2f}**",
        f"- Actions executed (retries/nudges the gate let through): **{len(acted_on)}**",
        f"- Simulated recovered amount: **Rs {recovered_paise/100:,.2f}** "
        f"({len(recovered)}/{len(acted_on)} executed actions 'succeeded' in simulation)",
        f"- LLM proposals the gate had to override (policy mismatch): "
        f"**{len(overridden)}/{len(results)}** "
        f"({len(overridden)/len(results)*100:.0f}% - the gate, not the model, is what makes this safe)",
        f"- Hard-blocked by gate (spending cap / duplicate): **{len(hard_blocked)}**",
        f"- Correctly refused as fraud-flagged (never retried): **{len(fraud_refused)}**",
        f"- Correctly identified as unrecoverable (no action taken): **{len(unrecoverable)}**",
        "",
    ]
    lines += render_decline_code_table(results)

    RESULTS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {RESULTS_PATH}")


if __name__ == "__main__":
    cli_args = parse_args()
    asyncio.run(run(inject_failure=cli_args.inject_failure))
