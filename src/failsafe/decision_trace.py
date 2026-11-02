"""
Decision Trace: a flight recorder for every payment FailSafe touched.

Builds TRACE.html, one self-contained offline page (no server, no CDN) from an
audit log. For each record it shows the full chain the pipeline went through:
the raw bank signal, the diagnosis, what the LLM proposed and why, every gate
check in order (which one decided, which were never reached), the MCP tool
call and its result, and a replay verdict: does the deterministic gate,
re-run today on the recorded inputs, reach the same decision?

Usage:
    python -m failsafe.decision_trace                          # logs/audit_log.jsonl -> TRACE.html
    python -m failsafe.decision_trace --audit logs/audit_log_onetime.jsonl \\
        --data data/failed_onetime_payments.json --out TRACE_onetime.html
"""

from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path

from failsafe.decline_codes import DeclineCode, load_decline_codes
from failsafe.gate import GATE_CHECKS, deciding_check
from failsafe.replay import (
    DEFAULT_AUDIT,
    DEFAULT_DATA,
    DEFAULT_POLICY,
    EVENT_LABELS,
    MONEY_ACTIONS,
    REPO,
    load_dataset_index,
    load_events,
    record_id_of,
    replay,
    segment_runs,
)

DEFAULT_OUT = REPO / "TRACE.html"

# Record-level event types that end up as steps. Anything else (run markers,
# demo markers) is run-level and not part of a record's trace.
STEP_EVENTS = set(EVENT_LABELS) | {"mcp_tool_error"}

VERDICT_EXECUTED = "executed"
VERDICT_ESCALATED = "escalated"
VERDICT_BLOCKED = "blocked"
VERDICT_REVIEW = "review"


def _parse_result(result) -> dict:
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            obj = json.loads(result)
            return obj if isinstance(obj, dict) else {"text": result}
        except json.JSONDecodeError:
            return {"text": result}
    return {}


def gate_ladder(event: dict) -> list[dict]:
    """Every gate check for one gate_decision event, in evaluation order, with
    status pass / override / decided / skipped."""
    stop = deciding_check(event.get("gate_reason", ""))
    out = []
    reached = True
    for name, label in GATE_CHECKS:
        if not reached:
            status = "skipped"
        elif name == "policy":
            status = "pass" if event.get("llm_matched_policy") else "override"
        elif name == stop:
            status = "decided"
            reached = False
        else:
            status = "pass"
        detail = ""
        if name == "policy":
            detail = f"{event.get('decline_code')} -> policy says {_policy_action(event)}"
        elif name == "attempt_cap":
            detail = f"{event.get('prior_attempt_count', 0)} prior attempts"
        elif name == "staleness" and event.get("halted_days_ago") is not None:
            detail = f"halted {event.get('halted_days_ago')} days"
        elif name == "spend_cap" and event.get("amount_paise") is not None:
            detail = f"INR {event['amount_paise'] / 100:,.2f}"
        out.append({"name": name, "label": label, "status": status, "detail": detail})
    return out


def _policy_action(event: dict) -> str:
    # When the policy step matched, the LLM's proposal IS the policy action.
    if event.get("llm_matched_policy"):
        return str(event.get("llm_proposed_action"))
    return _POLICY_CACHE.get(event.get("decline_code", ""), "?")


_POLICY_CACHE: dict[str, str] = {}


def _step(event: dict) -> dict:
    et = event.get("event_type", "")
    step = {
        "type": et,
        "title": EVENT_LABELS.get(et, et.replace("_", " ")),
        "ts": event.get("timestamp", ""),
        "lines": [],
        "tone": "info",
    }
    if et == "detection_decision":
        step["lines"] = [f"classified {event.get('classification')}", event.get("reasoning", "")]
    elif et == "diagnosis":
        ok = event.get("diagnosis_matched_ground_truth")
        step["signal"] = event.get("raw_decline_message", "")
        step["lines"] = [
            f"diagnosed {event.get('diagnosed_decline_code')}"
            + (
                ""
                if ok is None
                else (
                    " (matches ground truth)"
                    if ok
                    else f" (ground truth: {event.get('true_decline_code')})"
                )
            ),
            event.get("diagnosis_reasoning", ""),
        ]
        step["tone"] = "ok" if ok is not False else "warn"
    elif et == "gate_decision":
        step["title"] = "Gate"
        step["proposal"] = {
            "action": event.get("llm_proposed_action"),
            "reasoning": event.get("llm_reasoning", ""),
        }
        step["checks"] = gate_ladder(event)
        step["final"] = event.get("final_action")
        step["execute"] = bool(event.get("gate_execute"))
        step["reason"] = event.get("gate_reason", "")
        step["tone"] = "ok" if event.get("gate_execute") else "bad"
    elif et == "mcp_tool_call":
        res = _parse_result(event.get("result"))
        step["title"] = f"MCP {event.get('tool')}"
        step["args"] = event.get("arguments", {})
        keep = {
            k: res[k]
            for k in (
                "id",
                "status",
                "short_url",
                "flagged",
                "simulated",
                "reason",
                "error",
                "text",
            )
            if k in res
        }
        step["result"] = keep
        step["tone"] = "bad" if "error" in res else "ok"
    else:
        step["lines"] = [
            str(event.get(k))
            for k in ("note", "reason", "error", "raw_action", "raw_diagnosed_code")
            if event.get(k)
        ]
        step["tone"] = "warn"
    step["lines"] = [x for x in step["lines"] if x]
    return step


def _verdict(steps: list[dict]) -> tuple[str, str | None]:
    gate = next((s for s in reversed(steps) if s["type"] == "gate_decision"), None)
    if gate is None:
        return VERDICT_REVIEW, None
    if any(
        s["type"] == "mcp_tool_error" or (s["type"] == "mcp_tool_call" and s.get("tone") == "bad")
        for s in steps
    ):
        return VERDICT_BLOCKED, gate["final"]
    if not gate["execute"]:
        return VERDICT_BLOCKED, gate["final"]
    if gate["final"] in MONEY_ACTIONS:
        return VERDICT_EXECUTED, gate["final"]
    return VERDICT_ESCALATED, gate["final"]


def build_traces(
    events: list[dict], policy: dict[str, DeclineCode] | None = None, dataset: dict | None = None
) -> list[dict]:
    """One trace per (run, record). A record decided twice inside one run
    segment (a restart before run_id existed) gets one trace per decision."""
    _POLICY_CACHE.clear()
    if policy:
        _POLICY_CACHE.update({k: v.allowed_action.value for k, v in policy.items()})
    rep = replay(events, policy, dataset or {})
    outcomes = iter(rep.outcomes)
    traces: list[dict] = []
    for run_index, run in enumerate(segment_runs(events)):
        open_traces: dict[str, dict] = {}
        for e in run:
            if e.get("event_type") not in STEP_EVENTS:
                continue
            rid = record_id_of(e)
            if rid is None:
                continue
            id_field, record_id = rid
            t = open_traces.get(record_id)
            # A new trace starts at the first event of a record, or when the
            # current one already holds a finished gate decision and a new
            # diagnosis/proposal begins.
            if t is None or (
                t["closed"] and e.get("event_type") not in ("mcp_tool_call", "mcp_tool_error")
            ):
                t = {
                    "run": run_index,
                    "id_field": id_field,
                    "record_id": record_id,
                    "steps": [],
                    "closed": False,
                }
                open_traces[record_id] = t
                traces.append(t)
            if e.get("event_type") == "gate_decision":
                # Old logs did not record the gate's amount / staleness inputs;
                # show the dataset's values so the check ladder is complete.
                src_row = (dataset or {}).get(record_id, {})
                e = {**e}
                for k in ("amount_paise", "halted_days_ago"):
                    if e.get(k) is None and src_row.get(k) is not None:
                        e[k] = src_row[k]
            t["steps"].append(_step(e))
            if e.get("event_type") == "gate_decision":
                t["closed"] = True
                t["code"] = e.get("decline_code")
                t["amount_paise"] = e.get("amount_paise")
                t["overridden"] = not e.get("llm_matched_policy")
                t["check"] = deciding_check(e.get("gate_reason", ""))
                o = next(outcomes, None)
                if o is not None and o.record_id == record_id:
                    t["replay"] = {
                        "status": o.status,
                        "action": o.replay_action,
                        "reason": o.replay_reason,
                    }
            elif e.get("event_type") in (
                "diagnosis_failed",
                "unknown_decline_code",
                "circuit_open_short_circuit",
                "record_processing_error",
            ):
                t["closed"] = True
                t["code"] = e.get("decline_code") or e.get("true_decline_code")
    for t in traces:
        t.pop("closed", None)
        t["verdict"], t["final"] = _verdict(t["steps"])
        t.setdefault("overridden", False)
        t.setdefault("check", "pipeline")
        t.setdefault("replay", {"status": "n/a"})
    return traces


def summarize(traces: list[dict]) -> dict:
    latest = latest_per_record(traces)
    verdicts = Counter(t["verdict"] for t in latest)
    replays = Counter(t["replay"]["status"] for t in traces)
    collected = sum(t.get("amount_paise") or 0 for t in latest if t["verdict"] == VERDICT_EXECUTED)
    return {
        "records": len(latest),
        "decisions": len(traces),
        "runs": len({t["run"] for t in traces}),
        "executed": verdicts[VERDICT_EXECUTED],
        "escalated": verdicts[VERDICT_ESCALATED],
        "blocked": verdicts[VERDICT_BLOCKED],
        "review": verdicts[VERDICT_REVIEW],
        "overridden": sum(1 for t in latest if t["overridden"]),
        "reproduced": replays["reproduced"],
        "replayed": replays["reproduced"] + replays["changed"],
        "collected_paise": collected,
    }


def latest_per_record(traces: list[dict]) -> list[dict]:
    last: dict[str, dict] = {}
    for t in traces:
        last[t["record_id"]] = t
    return list(last.values())


def _json_for_script(obj) -> str:
    # Safe inside <script type="application/json">: no "</script>" breakout,
    # no HTML comment openers.
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_html(traces: list[dict], source: str, policy_path: str) -> str:
    stats = summarize(traces)
    payload = {"traces": traces, "stats": stats, "source": source, "policy": policy_path}
    template = (Path(__file__).parent / "assets" / "trace_template.html").read_text(
        encoding="utf-8"
    )
    return (
        template.replace("{{SOURCE}}", html.escape(source))
        .replace("{{POLICY}}", html.escape(policy_path))
        .replace("{{DATA}}", _json_for_script(payload))
    )


def build(audit: Path, out: Path, policy_path: Path, data: Path | None) -> dict:
    events = load_events(audit)
    policy = load_decline_codes(policy_path)
    dataset = load_dataset_index(data)
    traces = build_traces(events, policy, dataset)
    out.write_text(render_html(traces, _rel(audit), _rel(policy_path)), encoding="utf-8")
    return summarize(traces)


def _rel(p: Path) -> str:
    try:
        return p.resolve().relative_to(REPO).as_posix()
    except ValueError:
        return p.name


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m failsafe.decision_trace",
        description="Build TRACE.html, the per-record decision timeline.",
    )
    ap.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    args = ap.parse_args(argv)
    if not args.audit.exists():
        ap.error(f"audit log not found: {args.audit}")
    s = build(args.audit, args.out, args.policy, args.data)
    print(
        f"Wrote {args.out}: {s['records']} records, {s['decisions']} decisions over {s['runs']} runs; "
        f"{s['reproduced']}/{s['replayed']} decisions reproduce under replay."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
