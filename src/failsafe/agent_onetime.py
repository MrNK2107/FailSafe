import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from mcp import Client

from failsafe.audit_log import AuditLogger
from failsafe.decline_codes import RecoveryAction
from failsafe.gate import Gate
from failsafe.mcp_server import seed_tool_level_guard
from failsafe.mcp_server import server as mcp_server
from failsafe.mcp_tool_errors import render_tool_error_lines
from failsafe.ollama_client import require_ollama
from failsafe.paths import DATA_DIR, LOGS_DIR, RESULTS_DIR
from failsafe.recovery_pipeline import (
    process_record,
    render_corrected_override_line,
    render_decline_code_table,
    reset_circuit_breaker,
)

DATA_PATH = DATA_DIR / "failed_onetime_payments.json"
AUDIT_PATH = LOGS_DIR / "audit_log_onetime.jsonl"
CHECKPOINT_PATH = LOGS_DIR / "results_checkpoint_onetime.jsonl"
RESULTS_PATH = RESULTS_DIR / "RESULTS_ONETIME.md"

ID_FIELD = "payment_id"

SITUATION = (
    "A one-time payment failed at checkout. Unlike a subscription, Razorpay "
    "has no automatic retry cycle for one-time payments - this failure is "
    "immediate and has not been retried by anything yet."
)

ACTION_TO_TOOL = {
    RecoveryAction.IMMEDIATE_RETRY: "create_retry_order",
    RecoveryAction.DELAYED_RETRY: "create_retry_order",
    RecoveryAction.PAYMENT_LINK_NUDGE: "create_payment_link",
    RecoveryAction.NO_ACTION_FRAUD: "flag_for_manual_review",
    RecoveryAction.NO_ACTION_UNRECOVERABLE: "flag_for_manual_review",
}


async def process_one(client: Client, gate: Gate, audit: AuditLogger, pay: dict) -> dict:
    return await process_record(
        client,
        gate,
        audit,
        pay,
        id_field=ID_FIELD,
        action_to_tool=ACTION_TO_TOOL,
        item_label_field="item",
        situation=SITUATION,
        record_label="Payment",
    )


def _tool_keys_from_checkpoint(results: list[dict]) -> set[tuple[str, str]]:
    """(record_id, tool_name) pairs for money-moving tools a checkpointed
    run already executed - the keys mcp_server's tool-level guard uses."""
    keys = set()
    for r in results:
        if not r.get("gate_executed"):
            continue
        try:
            tool = ACTION_TO_TOOL[RecoveryAction(r.get("final_action"))]
        except (ValueError, KeyError):
            continue
        if tool != "flag_for_manual_review":
            keys.add((r["payment_id"], tool))
    return keys


def _load_checkpoint() -> list[dict]:
    if not CHECKPOINT_PATH.exists():
        return []
    results = []
    with CHECKPOINT_PATH.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                print(f"[checkpoint] warning: skipping corrupted line in {CHECKPOINT_PATH}")
                continue
            # A record short-circuited by an open LLM circuit breaker was never
            # evaluated by the gate - don't treat it as done, retry it on resume.
            if entry.get("circuit_open"):
                continue
            results.append(entry)
    return results


def _append_checkpoint(result: dict):
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(result, default=str) + "\n"
    if not CHECKPOINT_PATH.exists():
        with CHECKPOINT_PATH.open("w", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        return
    fd, tmp_path = tempfile.mkstemp(dir=str(CHECKPOINT_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            with CHECKPOINT_PATH.open(encoding="utf-8") as src:
                tmp.write(src.read())
            tmp.write(line)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, CHECKPOINT_PATH)
        with CHECKPOINT_PATH.open("a", encoding="utf-8") as f:
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


async def run():
    if not DATA_PATH.exists():
        raise SystemExit("No data found. Run `python -m failsafe.generate_data_onetime` first.")
    reset_circuit_breaker()  # never inherit a tripped breaker from a previous in-process run

    payments = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    gate = Gate()
    audit = AuditLogger(AUDIT_PATH)
    results = _load_checkpoint()
    done_ids = {r["payment_id"] for r in results}
    remaining = [p for p in payments if p["payment_id"] not in done_ids]
    acted_actions = {"immediate_retry", "delayed_retry", "payment_link_nudge"}
    already_spent = sum(
        r["amount_paise"]
        for r in results
        if r["final_action"] in acted_actions and r["gate_executed"]
    )
    already_seen_keys = {
        (r["payment_id"], r["final_action"]) for r in results if r["gate_executed"]
    }
    gate.seed_from_checkpoint(already_spent, already_seen_keys)
    # The MCP tool-level guard is process-global: reset it so nothing leaks
    # in from a previous in-process run, then restore it from the checkpoint
    # exactly like the gate above.
    seed_tool_level_guard(already_spent, _tool_keys_from_checkpoint(results))
    audit.log(
        "run_started",
        total_payments=len(payments),
        already_done_from_checkpoint=len(done_ids),
        remaining=len(remaining),
    )
    print(f"{len(done_ids)} already done (checkpoint), {len(remaining)} remaining")
    print(f"Processing {len(remaining)} failed one-time payments...")

    async with Client(mcp_server) as client:
        for pay in remaining:
            try:
                result = await process_one(client, gate, audit, pay)
            except Exception as e:
                audit.log("record_processing_error", payment_id=pay["payment_id"], error=str(e))
                result = {
                    "payment_id": pay["payment_id"],
                    "amount_paise": pay["amount_paise"],
                    "decline_code": pay["decline_code"],
                    "final_action": "no_action_unrecoverable",
                    "gate_executed": False,
                    "llm_matched_policy": False,
                    "llm_proposed": False,
                    "simulated_customer_response": False,
                }
            results.append(result)
            _append_checkpoint(result)
            print(f"{pay['payment_id']}  {pay['decline_code']:24s} -> {result['final_action']}")

    audit.log("run_finished", total_processed=len(results))
    write_results(results)


def write_results(results: list[dict]):
    if not results:
        RESULTS_PATH.write_text("# RESULTS_ONETIME\n\nNo records processed.\n", encoding="utf-8")
        print(f"\nWrote {RESULTS_PATH} (empty run)")
        return

    acted_actions = {"immediate_retry", "delayed_retry", "payment_link_nudge"}
    total_paise = sum(r["amount_paise"] for r in results)
    acted_on = [r for r in results if r["final_action"] in acted_actions and r["gate_executed"]]
    recovered = [r for r in acted_on if r["simulated_customer_response"]]
    recovered_paise = sum(r["amount_paise"] for r in recovered)
    overridden = [r for r in results if not r["llm_matched_policy"]]

    lines = [
        "# RESULTS_ONETIME",
        "",
        "Stretch goal: the same gate/policy/audit-log pattern applied to failed",
        "ONE-TIME payments instead of halted subscriptions - see `agent_onetime.py`",
        "and BUILD_LOG.md §13. Same honesty caveat as RESULTS.md: "
        "`simulated_customer_response` is a labeled synthetic assumption, not a",
        "real payment outcome.",
        "",
        f"- Total failed one-time payments processed: **{len(results)}**",
        f"- Total value: **Rs {total_paise / 100:,.2f}**",
        f"- Actions executed (retries/nudges the gate let through): **{len(acted_on)}**",
        f"- Simulated recovered amount: **Rs {recovered_paise / 100:,.2f}** "
        f"({len(recovered)}/{len(acted_on)} executed actions 'succeeded' in simulation)",
        f"- LLM proposals the gate had to override: **{len(overridden)}/{len(results)}** "
        f"({len(overridden) / len(results) * 100:.0f}%)",
        render_corrected_override_line(results),
        *render_tool_error_lines(results),
        "",
    ]
    lines += render_decline_code_table(results)

    RESULTS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {RESULTS_PATH}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m failsafe.agent_onetime",
        description="Run the failed one-time payment recovery agent.",
    )
    parser.add_argument(
        "--skip-healthcheck", action="store_true", help="skip the Ollama health check"
    )
    args = parser.parse_args(argv)
    require_ollama(skip=args.skip_healthcheck)
    asyncio.run(run())


if __name__ == "__main__":
    main()
