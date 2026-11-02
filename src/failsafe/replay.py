"""
Replay: re-run every recorded gate decision through the real, deterministic
Gate and compare. Three uses, one engine:

1. **Determinism check.** Replay an audit log under the policy it was written
   with: every decision should come out the same. A mismatch means the gate's
   code changed behaviour, which should never ship by accident.
2. **Policy what-if.** Replay under a *candidate* policy file before it is
   merged: which records would flip, to what, and how much money moves into
   or out of automated collection. CI posts this as a job summary on any PR
   that touches config/decline_policy.json.
3. **Incident report.** `--record <id>` prints the full story of one record
   (signal, diagnosis, proposal, every gate check, tool call) plus a replay
   verdict, as Markdown you can paste into a ticket.

Only the gate is replayed. The LLM's recorded proposal and diagnosis are
inputs, read back from the log, so a replay never calls Ollama or Razorpay
and is safe to run anywhere.

Usage:
    python -m failsafe.replay                                   # determinism check
    python -m failsafe.replay --policy candidate.json           # what-if
    python -m failsafe.replay --record sub_2c794247ffee4f       # incident report
    python -m failsafe.replay --policy new.json --baseline old.json --summary-md out.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from pathlib import Path

from failsafe.decline_codes import DeclineCode, RecoveryAction, load_decline_codes
from failsafe.gate import GATE_CHECKS, Gate, deciding_check
from failsafe.paths import PROJECT_ROOT

REPO = PROJECT_ROOT
DEFAULT_AUDIT = REPO / "logs" / "audit_log.jsonl"
DEFAULT_POLICY = REPO / "config" / "decline_policy.json"
DEFAULT_DATA = REPO / "data" / "halted_subscriptions.json"

ID_FIELDS = ("subscription_id", "payment_id")
MONEY_ACTIONS = {
    RecoveryAction.IMMEDIATE_RETRY.value,
    RecoveryAction.DELAYED_RETRY.value,
    RecoveryAction.PAYMENT_LINK_NUDGE.value,
}

CHECK_LABELS = {name: label for name, label in GATE_CHECKS} | {
    "passed": "all checks passed (policy action)"
}

REPRODUCED = "reproduced"
CHANGED = "changed"
UNREPLAYABLE = "unreplayable"


# ---------------------------------------------------------------- loading


def load_events(path: str | Path) -> list[dict]:
    """Audit events in file order. Blank and corrupt lines are skipped (the
    log is append-only; a crash can leave a torn last line)."""
    events = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def load_dataset_index(path: str | Path | None) -> dict[str, dict]:
    """record id -> dataset record. Old audit logs did not record the gate's
    amount / halted_days_ago inputs, so replay reads them from the dataset."""
    if path is None or not Path(path).exists():
        return {}
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    index = {}
    for row in rows:
        for f in ID_FIELDS:
            if f in row:
                index[row[f]] = row
                break
    return index


def record_id_of(event: dict) -> tuple[str, str] | None:
    for f in ID_FIELDS:
        if event.get(f):
            return f, event[f]
    return None


def segment_runs(events: Iterable[dict]) -> list[list[dict]]:
    """Split a log into runs. Newer logs carry run_id; older ones only have
    run_started markers, so a run_started (or a change of run_id) starts a
    new segment."""
    runs: list[list[dict]] = []
    current: list[dict] = []
    current_id: object = object()
    for e in events:
        rid = e.get("run_id")
        starts = e.get("event_type") == "run_started" or (
            rid is not None and rid != current_id and current
        )
        if starts and current:
            runs.append(current)
            current = []
        current.append(e)
        if rid is not None:
            current_id = rid
    if current:
        runs.append(current)
    return runs


# ---------------------------------------------------------------- replay


@dataclass
class Outcome:
    run_index: int
    id_field: str
    record_id: str
    decline_code: str
    proposed_action: str
    amount_paise: int | None
    prior_attempt_count: int
    halted_days_ago: int | None
    recorded_action: str
    recorded_execute: bool
    recorded_reason: str
    replay_action: str | None = None
    replay_execute: bool | None = None
    replay_reason: str = ""
    replay_matched_llm: bool | None = None
    status: str = REPRODUCED
    timestamp: str | None = None

    @property
    def recorded_check(self) -> str:
        return deciding_check(self.recorded_reason)

    @property
    def replay_check(self) -> str:
        return deciding_check(self.replay_reason) if self.replay_reason else ""

    @property
    def recorded_collects(self) -> bool:
        return self.recorded_execute and self.recorded_action in MONEY_ACTIONS

    @property
    def replay_collects(self) -> bool:
        return bool(self.replay_execute) and self.replay_action in MONEY_ACTIONS


@dataclass
class ReplayReport:
    policy_path: str
    audit_path: str
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def changed(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status == CHANGED]

    @property
    def unreplayable(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status == UNREPLAYABLE]

    @property
    def reproduced(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status == REPRODUCED]

    def money_delta_paise(self) -> tuple[int, int]:
        """(paise newly sent to automated collection, paise pulled out of it)."""
        gained = sum(
            o.amount_paise or 0
            for o in self.changed
            if o.replay_collects and not o.recorded_collects
        )
        lost = sum(
            o.amount_paise or 0
            for o in self.changed
            if o.recorded_collects and not o.replay_collects
        )
        return gained, lost

    def rerouted_paise(self) -> tuple[int, int]:
        """(records, paise) still collected automatically but by a different action."""
        moved = [o for o in self.changed if o.recorded_collects and o.replay_collects]
        return len(moved), sum(o.amount_paise or 0 for o in moved)

    def llm_agreement(self) -> tuple[int, int]:
        """(recorded LLM proposals this policy agrees with, proposals scored)."""
        scored = [o for o in self.outcomes if o.replay_matched_llm is not None]
        return sum(1 for o in scored if o.replay_matched_llm), len(scored)

    def transitions(self) -> Counter:
        return Counter((o.recorded_action, o.replay_action) for o in self.changed)


def _int_or_none(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def replay(
    events: list[dict],
    policy: Mapping[str, DeclineCode] | None = None,
    dataset: Mapping[str, dict] | None = None,
    policy_path: str = "",
    audit_path: str = "",
) -> ReplayReport:
    """Re-run each gate_decision through a fresh Gate per run (so spending caps
    and idempotency see the same in-run state they saw the first time)."""
    dataset = dataset or {}
    report = ReplayReport(policy_path=policy_path, audit_path=audit_path)
    for run_index, run in enumerate(segment_runs(events)):
        gate = Gate(policy=policy)
        seen: set[str] = set()
        for e in run:
            if e.get("event_type") != "gate_decision":
                continue
            rid = record_id_of(e)
            if rid is None:
                continue
            id_field, record_id = rid
            # Logs written before run_id existed have no marker when a process
            # restarted. A record decided twice in one segment without the
            # original gate calling it a duplicate means the original gate was
            # a fresh one, so the replay starts a fresh one there too.
            if record_id in seen and deciding_check(e.get("gate_reason", "")) != "idempotency":
                gate = Gate(policy=policy)
                seen = set()
            seen.add(record_id)
            source = dataset.get(record_id, {})
            amount = _int_or_none(e.get("amount_paise"))
            if amount is None:
                amount = _int_or_none(source.get("amount_paise"))
            halted = (
                _int_or_none(e.get("halted_days_ago"))
                if "halted_days_ago" in e
                else _int_or_none(source.get("halted_days_ago"))
            )
            o = Outcome(
                run_index=run_index,
                id_field=id_field,
                record_id=record_id,
                decline_code=e.get("decline_code", ""),
                proposed_action=e.get("llm_proposed_action", ""),
                amount_paise=amount,
                prior_attempt_count=int(e.get("prior_attempt_count") or 0),
                halted_days_ago=halted,
                recorded_action=e.get("final_action", ""),
                recorded_execute=bool(e.get("gate_execute")),
                recorded_reason=e.get("gate_reason", ""),
                timestamp=e.get("timestamp"),
            )
            if amount is None:
                o.status = UNREPLAYABLE
                o.replay_reason = "No amount_paise in the log or the dataset - pass --data."
                report.outcomes.append(o)
                continue
            try:
                proposed = RecoveryAction(o.proposed_action)
            except ValueError:
                proposed = RecoveryAction.NO_ACTION_UNRECOVERABLE
            try:
                d = gate.evaluate(
                    subscription_id=record_id,
                    decline_code=o.decline_code,
                    proposed_action=proposed,
                    amount_paise=amount,
                    prior_attempt_count=o.prior_attempt_count,
                    halted_days_ago=halted,
                )
            except KeyError:
                # The candidate policy dropped this code. The live pipeline
                # sends unknown codes to a human before the gate runs.
                o.status = CHANGED
                o.replay_action = RecoveryAction.NO_ACTION_UNRECOVERABLE.value
                o.replay_execute = True
                o.replay_reason = (
                    f"No policy entry for decline_code '{o.decline_code}' - needs human review."
                )
                report.outcomes.append(o)
                continue
            o.replay_action = d.final_action.value
            o.replay_execute = d.execute
            o.replay_reason = d.reason
            o.replay_matched_llm = d.llm_matched_policy
            same = o.replay_action == o.recorded_action and o.replay_execute == o.recorded_execute
            o.status = REPRODUCED if same else CHANGED
            report.outcomes.append(o)
    return report


def apply_overrides(
    table: Mapping[str, DeclineCode], overrides: Mapping[str, str]
) -> dict[str, DeclineCode]:
    """A copy of `table` with some codes' allowed_action changed. Unknown codes
    or actions raise ValueError, so a typo never silently replays as a no-op."""
    out = dict(table)
    valid = {a.value for a in RecoveryAction}
    for code, action in overrides.items():
        if code not in out:
            raise ValueError(f"unknown decline code {code!r}")
        if action not in valid:
            raise ValueError(f"{code!r}: invalid action {action!r}; must be one of {sorted(valid)}")
        out[code] = dc_replace(out[code], allowed_action=RecoveryAction(action))
    return out


def summary_dict(report: ReplayReport, limit: int = 200) -> dict:
    """JSON-ready replay summary (console API)."""
    gained, lost = report.money_delta_paise()
    n_moved, moved = report.rerouted_paise()
    agree, scored = report.llm_agreement()
    return {
        "decisions": len(report.outcomes),
        "reproduced": len(report.reproduced),
        "changed": len(report.changed),
        "unreplayable": len(report.unreplayable),
        "money_newly_collected_paise": gained,
        "money_pulled_back_paise": lost,
        "rerouted": {"records": n_moved, "paise": moved},
        "llm_agreement": {"agree": agree, "scored": scored},
        "transitions": [
            {"from": a, "to": b, "records": n} for (a, b), n in report.transitions().most_common()
        ],
        "changes": [
            {
                "record_id": o.record_id,
                "decline_code": o.decline_code,
                "recorded": o.recorded_action,
                "replayed": o.replay_action,
                "reason": o.replay_reason,
                "amount_paise": o.amount_paise,
                "run": o.run_index,
            }
            for o in report.changed[:limit]
        ],
    }


# ---------------------------------------------------------------- rendering


def _rupees(paise: int) -> str:
    return f"INR {paise / 100:,.2f}"


def render_summary(
    report: ReplayReport, title: str = "FailSafe policy replay", limit: int = 25
) -> str:
    """Markdown summary (CI job summary, PR description, terminal)."""
    total = len(report.outcomes)
    gained, lost = report.money_delta_paise()
    lines = [
        f"## {title}",
        "",
        f"Replayed **{total}** recorded gate decisions from `{report.audit_path}` against `{report.policy_path}`.",
        "",
        "| Result | Decisions |",
        "|---|---|",
        f"| Reproduced exactly | {len(report.reproduced)} |",
        f"| Would change | {len(report.changed)} |",
        f"| Not replayable (inputs missing) | {len(report.unreplayable)} |",
        "",
    ]
    if report.changed:
        n_moved, moved = report.rerouted_paise()
        lines += [
            f"- Newly sent to automated collection: **{_rupees(gained)}**",
            f"- Pulled out of automated collection (to a human / no action): **{_rupees(lost)}**",
            f"- Still collected, but by a different action: **{n_moved}** records, {_rupees(moved)}",
            "",
            "| From | To | Records |",
            "|---|---|---|",
        ]
        for (a, b), n in report.transitions().most_common():
            lines.append(f"| `{a}` | `{b}` | {n} |")
        lines += [
            "",
            "| Record | Code | Recorded | Replayed | Decided by |",
            "|---|---|---|---|---|",
        ]
        for o in report.changed[:limit]:
            lines.append(
                f"| `{o.record_id}` | `{o.decline_code}` | `{o.recorded_action}` | `{o.replay_action}` | {CHECK_LABELS.get(o.replay_check, 'policy table')} |"
            )
        if len(report.changed) > limit:
            lines.append(f"| ... | | | | {len(report.changed) - limit} more |")
    else:
        lines.append("No decision changes. This policy and gate reproduce every recorded decision.")
    agree, scored = report.llm_agreement()
    if scored:
        lines += [
            "",
            f"The recorded LLM proposals agree with this policy on {agree}/{scored} decisions ({agree / scored:.0%}); the gate overrides the rest.",
        ]
    return "\n".join(lines) + "\n"


def render_text(
    report: ReplayReport, title: str = "FailSafe policy replay", limit: int = 10
) -> str:
    """Plain-text summary for a terminal (the Markdown one is for CI/PRs)."""
    gained, lost = report.money_delta_paise()
    n_moved, moved = report.rerouted_paise()
    lines = [
        f"== {title} ==",
        f"audit log  {report.audit_path}",
        f"policy     {report.policy_path}",
        "",
        f"{len(report.outcomes)} recorded gate decisions replayed: {len(report.reproduced)} reproduced, "
        f"{len(report.changed)} would change, {len(report.unreplayable)} not replayable",
    ]
    if report.changed:
        lines += [
            "",
            "Money",
            f"  newly sent to automated collection   {_rupees(gained)}",
            f"  pulled out of automated collection   {_rupees(lost)}",
            f"  still collected, different action    {n_moved} records, {_rupees(moved)}",
            "",
            "Transitions",
        ]
        for (a, b), n in report.transitions().most_common():
            lines.append(f"  {a:<24} -> {b:<26} {n:>4}")
        lines += [
            "",
            f"First {min(limit, len(report.changed))} of {len(report.changed)} changed records",
        ]
        for o in report.changed[:limit]:
            lines.append(f"  {o.record_id:<20} {o.decline_code:<28} -> {o.replay_action}")
    else:
        lines += [
            "",
            "No decision changes: this policy and gate reproduce every recorded decision.",
        ]
    agree, scored = report.llm_agreement()
    if scored:
        lines += [
            "",
            f"LLM agreement with this policy: {agree}/{scored} ({agree / scored:.0%}); the gate overrides the rest.",
        ]
    return "\n".join(lines) + "\n"


EVENT_LABELS = {
    "detection_decision": "Detection",
    "detection_failed": "Detection failed (fails safe: proceeds)",
    "diagnosis": "Diagnosis",
    "diagnosis_failed": "Diagnosis failed -> human review",
    "unknown_decline_code": "Unknown decline code -> human review",
    "llm_parse_failure": "LLM returned no usable proposal -> safest fallback",
    "llm_invalid_action": "LLM proposed an action outside the enum -> safest fallback",
    "circuit_open_short_circuit": "LLM circuit breaker open -> skipped",
    "gate_decision": "Gate decision",
    "mcp_tool_call": "MCP tool call",
    "record_processing_error": "Processing error",
}


def _check_ladder(reason: str, matched: bool | None) -> list[str]:
    stop = deciding_check(reason)
    lines = []
    reached = True
    for name, label in GATE_CHECKS:
        if not reached:
            mark = "not reached"
        elif name == "policy":
            mark = "matched the LLM" if matched else "overrode the LLM"
        elif name == stop:
            mark = "**decided**"
            reached = False
        else:
            mark = "pass"
        lines.append(f"   - {label}: {mark}")
    return lines


def incident_report(events: list[dict], record_id: str, report: ReplayReport | None = None) -> str:
    """Markdown incident report for one record across every run in the log."""
    runs = segment_runs(events)
    lines = [f"# Incident report: `{record_id}`", ""]
    found = False
    for i, run in enumerate(runs):
        mine = [e for e in run if (record_id_of(e) or ("", ""))[1] == record_id]
        if not mine:
            continue
        found = True
        lines += [f"## Run {i + 1} of {len(runs)} - {mine[0].get('timestamp', 'no timestamp')}", ""]
        for e in mine:
            et = e.get("event_type", "?")
            lines.append(f"- **{EVENT_LABELS.get(et, et)}** `{e.get('timestamp', '')}`")
            if et == "diagnosis":
                ok = "matches" if e.get("diagnosis_matched_ground_truth") else "does NOT match"
                lines.append(f'   - signal: "{e.get("raw_decline_message", "")}"')
                lines.append(
                    f"   - diagnosed `{e.get('diagnosed_decline_code')}` ({ok} ground truth `{e.get('true_decline_code')}`)"
                )
            elif et == "gate_decision":
                lines.append(
                    f"   - LLM proposed `{e.get('llm_proposed_action')}`: {e.get('llm_reasoning', '')}"
                )
                lines += _check_ladder(e.get("gate_reason", ""), e.get("llm_matched_policy"))
                verdict = "executed" if e.get("gate_execute") else "blocked"
                lines.append(
                    f"   - final: `{e.get('final_action')}` ({verdict}) - {e.get('gate_reason', '')}"
                )
            elif et == "mcp_tool_call":
                lines.append(f"   - `{e.get('tool')}` {json.dumps(e.get('arguments', {}))}")
                lines.append(f"   - result: {_short_result(e.get('result'))}")
            else:
                note = e.get("note") or e.get("reason") or e.get("error") or ""
                if note:
                    lines.append(f"   - {note}")
        lines.append("")
    if not found:
        return f"# Incident report: `{record_id}`\n\nNo events for this record in the audit log.\n"
    if report is not None:
        mine_o = [o for o in report.outcomes if o.record_id == record_id]
        lines += ["## Replay verdict", ""]
        for o in mine_o:
            if o.status == REPRODUCED:
                lines.append(
                    f"- Run {o.run_index + 1}: **reproduced** - the gate decides `{o.replay_action}` again ({o.replay_check}) under `{report.policy_path}`."
                )
            elif o.status == CHANGED:
                lines.append(
                    f"- Run {o.run_index + 1}: **would change** `{o.recorded_action}` -> `{o.replay_action}` under `{report.policy_path}`: {o.replay_reason}"
                )
            else:
                lines.append(f"- Run {o.run_index + 1}: not replayable: {o.replay_reason}")
        lines.append("")
    return "\n".join(lines)


def _short_result(result) -> str:
    if not isinstance(result, str):
        return json.dumps(result)
    try:
        obj = json.loads(result)
    except json.JSONDecodeError:
        return result[:160]
    if isinstance(obj, dict):
        keep = {
            k: obj[k]
            for k in ("id", "status", "short_url", "flagged", "simulated", "error")
            if k in obj
        }
        return json.dumps(keep or obj)
    return result[:160]


# ---------------------------------------------------------------- CLI


def _rel(p: Path) -> str:
    try:
        return p.resolve().relative_to(REPO).as_posix()
    except ValueError:
        return str(p)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m failsafe.replay",
        description="Replay recorded gate decisions deterministically (no LLM, no Razorpay).",
    )
    ap.add_argument(
        "--audit",
        type=Path,
        default=DEFAULT_AUDIT,
        help="audit log to replay (default: logs/audit_log.jsonl)",
    )
    ap.add_argument(
        "--policy",
        type=Path,
        default=DEFAULT_POLICY,
        help="policy to replay under (default: config/decline_policy.json)",
    )
    ap.add_argument(
        "--baseline",
        type=Path,
        help="only report decisions that differ between --baseline and --policy (PR what-if)",
    )
    ap.add_argument(
        "--data",
        type=Path,
        default=DEFAULT_DATA,
        help="dataset used to fill gate inputs that old logs did not record",
    )
    ap.add_argument("--record", help="print an incident report for one record id")
    ap.add_argument(
        "--summary-md",
        type=Path,
        help="also write the Markdown summary here (e.g. $GITHUB_STEP_SUMMARY)",
    )
    ap.add_argument(
        "--format",
        choices=("text", "md"),
        default="text",
        help="stdout format (the --summary-md file is always Markdown)",
    )
    ap.add_argument(
        "--fail-on-change", action="store_true", help="exit 1 if any decision would change"
    )
    args = ap.parse_args(argv)

    if not args.audit.exists():
        ap.error(f"audit log not found: {args.audit}")
    try:
        policy = load_decline_codes(args.policy)
        baseline = load_decline_codes(args.baseline) if args.baseline else None
    except (OSError, ValueError, KeyError) as e:
        print(f"policy error: {e}", file=sys.stderr)
        return 2

    events = load_events(args.audit)
    dataset = load_dataset_index(args.data)
    report = replay(
        events, policy, dataset, policy_path=_rel(args.policy), audit_path=_rel(args.audit)
    )

    if args.record:
        print(incident_report(events, args.record, report))
        return 0

    title = "FailSafe policy replay"
    if baseline is not None:
        base = replay(
            events, baseline, dataset, policy_path=_rel(args.baseline), audit_path=_rel(args.audit)
        )
        report = diff_reports(base, report)
        title = "FailSafe policy what-if (this change vs. baseline)"

    print(
        render_summary(report, title=title)
        if args.format == "md"
        else render_text(report, title=title)
    )
    if args.summary_md:
        with args.summary_md.open("a", encoding="utf-8") as f:
            f.write(render_summary(report, title=title))
    return 1 if (args.fail_on_change and report.changed) else 0


def diff_reports(base: ReplayReport, cand: ReplayReport) -> ReplayReport:
    """Treat the baseline policy's replay as 'recorded' and the candidate's as
    'replayed', so the summary shows only what this policy change moves."""
    out = ReplayReport(
        policy_path=f"{cand.policy_path} (vs {base.policy_path})", audit_path=cand.audit_path
    )
    for b, c in zip(base.outcomes, cand.outcomes, strict=True):
        if b.status == UNREPLAYABLE:
            out.outcomes.append(c)
            continue
        o = Outcome(**{**c.__dict__})
        o.recorded_action = b.replay_action or b.recorded_action
        o.recorded_execute = bool(b.replay_execute)
        o.recorded_reason = b.replay_reason
        same = o.recorded_action == c.replay_action and o.recorded_execute == c.replay_execute
        o.status = REPRODUCED if same else CHANGED
        out.outcomes.append(o)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
