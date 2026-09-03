"""
Wires the four standalone recovery domains - subscriptions (agent.py),
one-time payments (agent_onetime.py), checkout abandonment
(checkout_abandonment_agent.py), overdue receivables (receivables_agent.py)
- into ONE pipeline, closing the gap BUILD_LOG.md §16/§17 and README.md §6
repeatedly and honestly disclosed: "None of the four domains is wired into
a single integrated pipeline - they remain four separate, standalone
demonstrations."

What "integrated" means here, stated precisely because it's easy to
overclaim: this module does NOT merge Gate.evaluate() / AbandonmentGate.
evaluate() / ReceivableGate.evaluate() into one function, and does NOT
redefine any domain's audit-log schema or policy table. Those were kept
separate for good, already-documented reasons (see abandonment_gate.py's
and receivables_gate.py's own module docstrings: each gate's signature is
built around a different lookup key, and reshaping any of them risks the
already-passing test suite built around it). Reshaping them was explicitly
out of scope for this change.

What IS genuinely new: ONE entry point that takes a MIXED, interleaved
stream of records - any combination of the four domains' own record shapes,
in any order - and, for each record, identifies which domain it belongs to
purely from its shape (identify_domain, below) and routes it to that
domain's real, unmodified, already-tested `process_one()` function. All
four domains share ONE in-process MCP client session for the run (today
each domain's standalone script opens its own separate session in its own
separate process invocation - this is the actual "one pipeline" moment).
Each domain keeps its own gate instance, its own AuditLogger writing to its
own existing log path, and its own policy table, completely unmodified -
running this script appends real entries to the same four log files
(`logs/audit_log.jsonl`, `logs/audit_log_onetime.jsonl`,
`logs/audit_log_checkout_abandonment.jsonl`,
`logs/audit_log_receivables.jsonl`) that the four standalone scripts
already write to, exactly like re-running any one of them today already
does. A NEW cross-domain index log (`logs/audit_log_integrated.jsonl`) and
a NEW unified report (`INTEGRATED_RESULTS.md`) are added on top, without
touching any of the four domains' own `RESULTS*.md` files.

A real, disclosed consequence of running all four domains in ONE process,
not four: mcp_server.py's own tool-level defense-in-depth guard
(`_enforce_tool_level_cap`, `_tool_run_total_paise`) is process-global by
design (see that module's docstring) - so `MAX_RUN_TOTAL_PAISE` is now a
POOLED cap across all four domains' executed actions within a single
integrated run, not four independent per-domain totals the way four
separate process invocations would have. Each domain's own `Gate`/
`AbandonmentGate`/`ReceivableGate` instance still enforces its OWN
`_run_total_paise` independently (gate-level spending caps are NOT pooled -
only the tool-level guard is, since it's the one piece of process-global
state in mcp_server.py). At this script's default scale this is far below
`MAX_RUN_TOTAL_PAISE` (see BUILD_LOG.md §18 for the real measured total),
but it is a genuine behavior difference from running the four scripts
separately, not an oversight.

SIMULATE is forced True for this script's run(), the same two-patch fix
(`mcp_server.SIMULATE` plus `mcp_server._rp.simulate`) checkout_abandonment_
agent.py/receivables_agent.py already use and for the same disclosed
reason: this repo's own `.env` already has real `rzp_test_` keys
configured, and the real test-mode account has already exhausted its
payment-link quota. Direct programmatic use of any domain's own
`process_one()` is unaffected.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from mcp import Client

import mcp_server as _mcp_server_module
from mcp_server import server as mcp_server

from agent import process_one as _subscription_process_one
from agent_onetime import process_one as _onetime_process_one
from checkout_abandonment_agent import ACTIONABLE as _CHECKOUT_ACTIONABLE
from checkout_abandonment_agent import process_one as _checkout_process_one
from receivables_agent import ACTIONABLE as _RECEIVABLES_ACTIONABLE
from receivables_agent import process_one as _receivables_process_one
from abandonment_gate import AbandonmentGate
from audit_log import AuditLogger
from checkout_abandonment_policy import AbandonmentAction
from decline_codes import RecoveryAction
from gate import Gate
from receivables_gate import ReceivableGate
from receivables_policy import ReceivableAction
from recovery_pipeline import count_prior_attempts

DATA_DIR = Path(__file__).parent.parent / "data"
LOGS_DIR = Path(__file__).parent.parent / "logs"
INTEGRATED_AUDIT_PATH = LOGS_DIR / "audit_log_integrated.jsonl"
INTEGRATED_RESULTS_PATH = Path(__file__).parent.parent / "INTEGRATED_RESULTS.md"

_SUBSCRIPTION_LIKE_ACTIONABLE = {
    RecoveryAction.IMMEDIATE_RETRY.value,
    RecoveryAction.DELAYED_RETRY.value,
    RecoveryAction.PAYMENT_LINK_NUDGE.value,
}

# One entry per domain: how to load its data, how to build its gate, which
# real process_one() to call, its own audit-log path, and which final_action
# values that domain's own policy table treats as a real, gate-executed
# recovery action (reused directly from each domain's own module, never
# redefined here) - needed only for the unified report's aggregate counts.
DOMAINS: dict[str, dict] = {
    "subscription": {
        "id_field": "subscription_id",
        "data_path": DATA_DIR / "halted_subscriptions.json",
        "make_gate": Gate,
        "audit_path": LOGS_DIR / "audit_log.jsonl",
        "process_one": _subscription_process_one,
        "actionable_values": _SUBSCRIPTION_LIKE_ACTIONABLE,
        # Used only for a per-record dispatch/processing exception - each
        # domain's own "flag for a human, no automated action taken"
        # policy value, never a value foreign to that domain's own enum
        # (found on a later code review: an earlier version of this file
        # hardcoded the subscription-only value for all four domains).
        "fallback_action": RecoveryAction.NO_ACTION_UNRECOVERABLE.value,
    },
    "one_time_payment": {
        "id_field": "payment_id",
        "data_path": DATA_DIR / "failed_onetime_payments.json",
        "make_gate": Gate,
        "audit_path": LOGS_DIR / "audit_log_onetime.jsonl",
        "process_one": _onetime_process_one,
        "actionable_values": _SUBSCRIPTION_LIKE_ACTIONABLE,
        "fallback_action": RecoveryAction.NO_ACTION_UNRECOVERABLE.value,
    },
    "checkout_abandonment": {
        "id_field": "cart_id",
        "data_path": DATA_DIR / "abandoned_checkouts.json",
        "make_gate": AbandonmentGate,
        "audit_path": LOGS_DIR / "audit_log_checkout_abandonment.jsonl",
        "process_one": _checkout_process_one,
        "actionable_values": {a.value for a in _CHECKOUT_ACTIONABLE},
        "fallback_action": AbandonmentAction.NO_ACTION_NEEDS_HUMAN_REVIEW.value,
    },
    "overdue_receivable": {
        "id_field": "invoice_id",
        "data_path": DATA_DIR / "overdue_invoices.json",
        "make_gate": ReceivableGate,
        "audit_path": LOGS_DIR / "audit_log_receivables.jsonl",
        "process_one": _receivables_process_one,
        "actionable_values": {a.value for a in _RECEIVABLES_ACTIONABLE},
        "fallback_action": ReceivableAction.NO_ACTION_NEEDS_HUMAN_REVIEW.value,
    },
}


def identify_domain(record: dict) -> str:
    """
    Genuine polymorphic dispatch by record shape: exactly one of the four
    known domain ID fields must be present. Fails loudly on zero or 2+
    matches rather than guessing - this repo's established convention for
    an unrecognized/ambiguous input (decline_codes.py, every policy
    loader), applied here to routing instead of a lookup table.
    """
    matches = [name for name, cfg in DOMAINS.items() if cfg["id_field"] in record]
    if len(matches) != 1:
        raise ValueError(
            f"Cannot identify domain for record with keys {sorted(record.keys())}: "
            f"found {len(matches)} matching domain ID fields ({matches}), expected "
            f"exactly one of {[cfg['id_field'] for cfg in DOMAINS.values()]}."
        )
    return matches[0]


def _load_batches(per_domain: int) -> dict[str, list[dict]]:
    batches = {}
    for name, cfg in DOMAINS.items():
        if not cfg["data_path"].exists():
            raise SystemExit(
                f"No data found for domain '{name}' at {cfg['data_path']}. "
                f"Run the matching generate_*.py first."
            )
        records = json.loads(cfg["data_path"].read_text(encoding="utf-8"))
        batches[name] = records[:per_domain]
    return batches


def _interleave(batches: dict[str, list[dict]]) -> list[dict]:
    """Round-robin across domains (domain1 rec1, domain2 rec1, domain3 rec1,
    domain4 rec1, domain1 rec2, ...) - a deliberately mixed stream, not four
    sequential loops, so identify_domain() is doing real dispatch work on
    every step rather than just tracking which file it started with."""
    stream = []
    max_len = max((len(v) for v in batches.values()), default=0)
    for i in range(max_len):
        for name in DOMAINS:
            if i < len(batches[name]):
                stream.append(batches[name][i])
    return stream


async def run(per_domain: int = 15) -> dict[str, list[dict]]:
    batches = _load_batches(per_domain)
    stream = _interleave(batches)

    gates = {name: cfg["make_gate"]() for name, cfg in DOMAINS.items()}
    audits = {name: AuditLogger(cfg["audit_path"]) for name, cfg in DOMAINS.items()}
    integrated_audit = AuditLogger(INTEGRATED_AUDIT_PATH)
    integrated_audit.log(
        "integrated_run_started", total_records=len(stream), per_domain=per_domain
    )
    print(f"Processing {len(stream)} records across {len(DOMAINS)} domains (interleaved)...")

    # Cross-run history for the subscription domain's own attempt-cap
    # stopping rule (gate.py MAX_ATTEMPTS_PER_SUBSCRIPTION) - read once, up
    # front, exactly like agent.py's own run() does. This domain is the
    # only one of the four whose process_one() consumes prior_attempt_count
    # at all; without this, every subscription routed through this
    # dispatcher would look like a first attempt to the gate regardless of
    # its real history in logs/audit_log.jsonl (found on a later code
    # review - a real gap, not a cosmetic one, since it's the exact
    # cross-run stopping rule this project built and tested).
    subscription_audit_history = audits["subscription"].read_all()

    results: dict[str, list[dict]] = {name: [] for name in DOMAINS}

    with patch.object(_mcp_server_module, "SIMULATE", True), \
         patch.object(_mcp_server_module._rp, "simulate", True):
        async with Client(mcp_server) as client:
            for record in stream:
                # A record identify_domain() can't classify is skipped and
                # logged, not fatal to the whole run - mirrors this
                # project's own "one bad record must not take down a batch"
                # convention (agent.py's run()), applied to dispatch itself
                # rather than just to process_one() (found missing here on
                # a later code review).
                try:
                    domain = identify_domain(record)
                except ValueError as e:
                    integrated_audit.log(
                        "integrated_dispatch_failed",
                        error=str(e),
                        record_keys=sorted(record.keys()),
                    )
                    print(f"[dispatch-error] skipping unrecognized record: {e}")
                    continue

                cfg = DOMAINS[domain]
                record_id = record[cfg["id_field"]]
                extra_kwargs = {}
                if domain == "subscription":
                    extra_kwargs["prior_attempt_count"] = count_prior_attempts(
                        subscription_audit_history, cfg["id_field"], record_id
                    )
                try:
                    result = await cfg["process_one"](
                        client, gates[domain], audits[domain], record, **extra_kwargs
                    )
                except Exception as e:
                    audits[domain].log(
                        "record_processing_error", error=str(e), **{cfg["id_field"]: record_id}
                    )
                    result = {
                        cfg["id_field"]: record_id,
                        "amount_paise": record.get("amount_paise", 0),
                        "final_action": cfg["fallback_action"],
                        "gate_executed": False,
                        "diagnosis_matched_ground_truth": False,
                        "simulated_customer_response": False,
                        "tool_result": None,
                    }
                results[domain].append(result)
                integrated_audit.log(
                    "integrated_dispatch",
                    domain=domain,
                    record_id=record_id,
                    final_action=result["final_action"],
                    amount_paise=result.get("amount_paise"),
                    gate_executed=result.get("gate_executed"),
                )
                print(f"[{domain:22s}] {record_id:20s} -> {result['final_action']}")

    integrated_audit.log("integrated_run_finished", total_processed=len(stream))
    write_integrated_results(results)
    return results


def write_integrated_results(results: dict[str, list[dict]]):
    lines = [
        "# INTEGRATED_RESULTS",
        "",
        "One run, one shared in-process MCP client session, ONE mixed and",
        "interleaved stream of records drawn from all four recovery domains -",
        "dispatched per-record to the domain's own real, unmodified,",
        "already-tested `process_one()` purely by identifying which of the",
        "four known ID fields the record carries (`integrated_pipeline.py`'s",
        "`identify_domain()`). Each domain's gate, policy table, and audit",
        "log remain completely separate by design - see this file's module",
        "docstring and BUILD_LOG.md §18 for exactly what 'integrated' does",
        "and does not mean here. Same honesty caveat as every other results",
        "file in this repo: `simulated_customer_response` is a labeled",
        "synthetic assumption, not a real customer outcome.",
        "",
    ]

    grand_records = grand_value = grand_actions = grand_recovered = 0
    dispatch_errors = 0
    per_domain_rows = []

    for name, cfg in DOMAINS.items():
        recs = results.get(name, [])
        total = len(recs)
        total_value = sum(r.get("amount_paise") or 0 for r in recs)
        acted_on = [
            r for r in recs
            if r["final_action"] in cfg["actionable_values"] and r["gate_executed"]
        ]
        recovered = [r for r in acted_on if r.get("simulated_customer_response")]
        recovered_value = sum(r.get("amount_paise") or 0 for r in recovered)
        misrouted = sum(1 for r in recs if cfg["id_field"] not in r)

        grand_records += total
        grand_value += total_value
        grand_actions += len(acted_on)
        grand_recovered += recovered_value
        dispatch_errors += misrouted

        per_domain_rows.append((name, total, total_value, len(acted_on), recovered_value))

    lines += [
        f"- Records processed across all 4 domains (one mixed, interleaved stream): "
        f"**{grand_records}**",
        f"- Total value at risk across all domains: **Rs {grand_value/100:,.2f}**",
        f"- Actions executed across all domains: **{grand_actions}**",
        f"- Simulated recovered value across all domains: **Rs {grand_recovered/100:,.2f}**",
        f"- Dispatch correctness: **{grand_records - dispatch_errors}/{grand_records}** records "
        "carry the ID field their own bucket expects. This is 100% by construction, not a "
        "measured accuracy figure - `identify_domain()` raises before a record is ever "
        "processed if its domain can't be uniquely identified, so a record can never be "
        "silently misrouted; this line is a proof the guarantee held, not an estimate.",
        "",
        "## By domain",
        "",
        "| Domain | Records | Value at risk | Actions executed | Simulated recovered |",
        "|---|---|---|---|---|",
    ]
    for name, total, total_value, acted, recovered_value in per_domain_rows:
        lines.append(
            f"| {name} | {total} | Rs {total_value/100:,.2f} | {acted} | "
            f"Rs {recovered_value/100:,.2f} |"
        )

    INTEGRATED_RESULTS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {INTEGRATED_RESULTS_PATH}")
    print(f"Records: {grand_records}  Actions executed: {grand_actions}  "
          f"Recovered: Rs {grand_recovered/100:,.2f}")


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    asyncio.run(run(n))
