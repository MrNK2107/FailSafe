"""
Derive every reported metric directly from the append-only audit logs.

Run from the repo root::

    python scripts/metrics.py

Each domain is summarized against its OWN audit schema - the flagship and
one-time pipelines have an LLM action-proposal stage (so they report
llm_matched_policy), while checkout abandonment and receivables are
diagnosis-driven with a deterministic policy lookup (so they report
diagnosis accuracy instead). Definitions are deliberately conservative:
counted only when the log proves it.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

DOMAINS = [
    {
        "name": "Halted subscriptions (flagship)",
        "path": "logs/audit_log.jsonl",
        "id_field": "subscription_id",
        "gate_event": "gate_decision",
        "has_llm_stage": True,
        "diag_event": "diagnosis",
    },
    {
        "name": "One-time payments",
        "path": "logs/audit_log_onetime.jsonl",
        "id_field": "payment_id",
        "gate_event": "gate_decision",
        "has_llm_stage": True,
        "diag_event": None,
    },
    {
        "name": "Checkout abandonment",
        "path": "logs/audit_log_checkout_abandonment.jsonl",
        "id_field": "cart_id",
        "gate_event": "abandonment_gate_decision",
        "has_llm_stage": False,
        "diag_event": "abandonment_diagnosis",
    },
    {
        "name": "Overdue receivables",
        "path": "logs/audit_log_receivables.jsonl",
        "id_field": "invoice_id",
        "gate_event": "receivable_gate_decision",
        "has_llm_stage": False,
        "diag_event": "receivable_diagnosis",
    },
]

MONEY_TOOLS = {"create_payment_link", "create_retry_order"}


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def summarize(events: list[dict], cfg: dict) -> dict:
    id_field = cfg["id_field"]
    gate = [e for e in events if e.get("event_type") == cfg["gate_event"]]
    ids = {e.get(id_field) for e in gate if e.get(id_field)}

    s = {"records": len(ids), "gate_decisions": len(gate)}

    if cfg["has_llm_stage"]:
        s["llm_match"] = sum(1 for e in gate if e.get("llm_matched_policy") is True)
        s["overrides"] = sum(1 for e in gate if e.get("llm_matched_policy") is False)

    diag_event = cfg.get("diag_event")
    if diag_event:
        diag = [e for e in events if e.get("event_type") == diag_event]
        s["diagnosed"] = len(diag)
        s["diagnosis_match"] = sum(
            1 for e in diag if e.get("diagnosis_matched_ground_truth") is True
        )

    s["escalated"] = sum(
        1
        for e in gate
        if str(e.get("final_action", "")).startswith("no_action")
        and "Escalated" in (e.get("gate_reason") or "")
    )

    money = [
        e for e in events if e.get("event_type") == "mcp_tool_call" and e.get("tool") in MONEY_TOOLS
    ]
    # Dedupe (id, tool) -> max amount seen, so re-runs don't double-count value.
    per_key: dict[tuple, int] = {}
    for e in money:
        key = (e.get(id_field), e.get("tool"))
        per_key[key] = max(per_key.get(key, 0), (e.get("arguments") or {}).get("amount_paise", 0))
    s["money_actions"] = len(per_key)
    s["value_paise"] = sum(per_key.values())
    s["flagged"] = sum(
        1
        for e in events
        if e.get("event_type") == "mcp_tool_call" and e.get("tool") == "flag_for_manual_review"
    )
    return s


def report(cfg: dict) -> None:
    path = ROOT / cfg["path"]
    events = load(path)
    if not events:
        print(f"\n## {cfg['name']}\n(no log at {cfg['path']})")
        return
    s = summarize(events, cfg)
    print(f"\n## {cfg['name']}  ({cfg['path']})")
    print(f"- Records with gate decisions: {s['records']}")
    print(f"- Gate decisions: {s['gate_decisions']}")
    if "llm_match" in s and s["gate_decisions"]:
        pct = f" ({s['llm_match'] / s['gate_decisions']:.0%})"
        print(f"- LLM matched policy: {s['llm_match']}/{s['gate_decisions']}{pct}")
        print(f"- Gate overrides of LLM proposal: {s['overrides']}")
    if s.get("diagnosed"):
        pct = f" ({s['diagnosis_match'] / s['diagnosed']:.0%})"
        print(f"- Diagnosis matched ground truth: {s['diagnosis_match']}/{s['diagnosed']}{pct}")
    print(f"- Escalated to manual review (stopping rules): {s['escalated']}")
    print(f"- Executed money actions (unique id x tool): {s['money_actions']}")
    print(f"- Total value of executed money actions: Rs {s['value_paise'] / 100:,.2f}")
    print(f"- flag_for_manual_review calls: {s['flagged']}")


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] in {"-h", "--help"}:
        print(__doc__)
        return 0
    for cfg in DOMAINS:
        report(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
