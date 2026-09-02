"""
Generates REPORT.html: a single, self-contained, offline static file from
logs/audit_log.jsonl - no server, no framework, no CDN dependency, matching
this project's own "no live UI" scope decision (BUILD_LOG.md §8) while
still being watchable in 10 seconds on camera instead of scrolled through
as raw JSONL. Re-run any time after a batch run to refresh it.

Usage: python generate_report.py
"""

import html
import json
from pathlib import Path

AUDIT_PATH = Path(__file__).parent.parent / "logs" / "audit_log.jsonl"
REPORT_PATH = Path(__file__).parent.parent / "REPORT.html"


def _load_events() -> list[dict]:
    if not AUDIT_PATH.exists():
        raise SystemExit(f"No audit log found at {AUDIT_PATH}. Run agent.py first.")
    with AUDIT_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _bar(label: str, value: int, max_value: int, color: str) -> str:
    pct = (value / max_value * 100) if max_value else 0
    # decline_code/final_action are a small fixed enum today, but this is
    # a cheap, zero-downside defense-in-depth escape regardless - HTML is
    # built by string interpolation here, not a templating engine with
    # auto-escaping, so nothing else protects against a value containing
    # HTML-special characters if that ever changes.
    safe_label = html.escape(str(label))
    return (
        f'<div class="bar-row">'
        f'<span class="bar-label">{safe_label}</span>'
        f'<div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%;background:{color}"></div></div>'
        f'<span class="bar-value">{value}</span>'
        f"</div>"
    )


def build_report():
    events = _load_events()
    all_gate_decisions = [e for e in events if e["event_type"] == "gate_decision"]

    # logs/audit_log.jsonl is append-only across every run this project has
    # ever done (real cross-run memory - see gate.py's MAX_ATTEMPTS_PER_SUBSCRIPTION),
    # so a subscription reprocessed across multiple sessions has more than one
    # gate_decision event. This report should describe the CURRENT state of
    # each subscription, not a lifetime count of every decision ever made -
    # so keep only the latest event per subscription_id (file order is
    # chronological, so the last occurrence wins), matching the same
    # dedup-by-latest-state semantics results_checkpoint.jsonl already uses.
    latest_by_subscription: dict[str, dict] = {}
    for d in all_gate_decisions:
        latest_by_subscription[d["subscription_id"]] = d
    gate_decisions = list(latest_by_subscription.values())

    if not gate_decisions:
        REPORT_PATH.write_text(
            "<title>Report</title><p>No gate_decision events in the audit log yet. Run agent.py first.</p>",
            encoding="utf-8",
        )
        print(f"Wrote {REPORT_PATH} (empty - no gate_decision events yet)")
        return

    total = len(gate_decisions)
    matched = sum(1 for d in gate_decisions if d["llm_matched_policy"])
    mismatched = total - matched

    by_code: dict[str, dict] = {}
    for d in gate_decisions:
        c = by_code.setdefault(d["decline_code"], {"total": 0, "matched": 0})
        c["total"] += 1
        if d["llm_matched_policy"]:
            c["matched"] += 1

    final_action_counts: dict[str, int] = {}
    for d in gate_decisions:
        final_action_counts[d["final_action"]] = final_action_counts.get(d["final_action"], 0) + 1

    max_code_total = max(c["total"] for c in by_code.values())
    max_action_count = max(final_action_counts.values())

    code_rows = ""
    for code, c in sorted(by_code.items(), key=lambda kv: -kv[1]["total"]):
        rate = c["matched"] / c["total"] * 100
        color = "#12845A" if rate >= 70 else "#B98900" if rate >= 40 else "#D0342C"
        safe_code = html.escape(str(code))
        code_rows += (
            f'<tr data-code="{safe_code}">'
            f"<td>{safe_code}</td><td>{c['total']}</td><td>{c['matched']}</td>"
            f"<td>{c['total'] - c['matched']}</td>"
            f'<td><span class="pill" style="background:{color}1A;color:{color}">{rate:.0f}%</span></td>'
            f"</tr>"
        )

    action_bars = "".join(
        _bar(action, count, max_action_count, "#3395FF")
        for action, count in sorted(final_action_counts.items(), key=lambda kv: -kv[1])
    )
    code_bars = "".join(
        _bar(code, c["total"], max_code_total, "#6C4FD0")
        for code, c in sorted(by_code.items(), key=lambda kv: -kv[1]["total"])
    )

    page_html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Subscription Recovery Agent - Report</title>
<style>
  * {{ box-sizing:border-box; }}
  ::selection {{ background:#CFE6FF; color:#0B1E33; }}
  body {{ font: 14px/1.6 -apple-system, "Segoe UI", Roboto, sans-serif; background:#F6F8FB; color:#28384A; margin:0; padding:56px 64px; }}
  .page {{ max-width:920px; margin:0 auto; }}
  h1 {{ font-size:22px; font-weight:650; color:#0B1E33; margin:0 0 6px; letter-spacing:-.015em; }}
  .sub {{ color:#6B7A8C; margin-bottom:36px; font-size:13px; }}
  .stats {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:1px; background:#E4E9F0;
            border:1px solid #E4E9F0; border-radius:12px; overflow:hidden; margin-bottom:48px;
            box-shadow:0 1px 2px rgba(16,24,40,.04), 0 8px 24px -14px rgba(16,24,40,.14); }}
  .stat {{ background:#FFFFFF; padding:20px 24px; }}
  .stat .n {{ font-size:27px; font-weight:650; color:#0B1E33; font-variant-numeric:tabular-nums; }}
  .stat .l {{ color:#6B7A8C; font-size:11px; text-transform:uppercase; letter-spacing:.06em; margin-top:4px; }}
  h2 {{ font-size:12.5px; font-weight:650; color:#6B7A8C; text-transform:uppercase; letter-spacing:.07em; margin:40px 0 16px; }}
  .panel {{ background:#FFFFFF; border-radius:12px; padding:22px 24px;
            box-shadow:0 1px 2px rgba(16,24,40,.04), 0 8px 24px -16px rgba(16,24,40,.14); }}
  .bar-row {{ display:flex; align-items:center; gap:12px; margin:11px 0; }}
  .bar-row:first-child {{ margin-top:0; }}
  .bar-row:last-child {{ margin-bottom:0; }}
  .bar-label {{ width:200px; flex-shrink:0; color:#3B4A5A; font-size:13px; }}
  .bar-track {{ flex:1; background:#EEF2F7; border-radius:4px; height:8px; overflow:hidden; }}
  .bar-fill {{ height:100%; border-radius:4px; }}
  .bar-value {{ width:34px; text-align:right; color:#6B7A8C; font-size:12px; font-weight:650; font-variant-numeric:tabular-nums; }}
  table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }}
  th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid #EEF2F7; font-size:13px; }}
  th {{ color:#6B7A8C; font-weight:650; text-transform:uppercase; font-size:10.5px; letter-spacing:.06em; }}
  tbody tr:last-child td {{ border-bottom:none; }}
  tbody tr:hover td {{ background:#F9FBFD; }}
  .pill {{ padding:3px 10px; border-radius:99px; font-weight:650; font-size:11.5px; }}
  input#filter {{ background:#F6F8FB; border:1px solid #E4E9F0; color:#28384A; padding:9px 13px; border-radius:8px; width:280px; margin-bottom:14px; font-size:13px; }}
  input#filter:focus {{ outline:none; border-color:#3395FF; background:#FFFFFF; box-shadow:0 0 0 3px rgba(51,149,255,.15); }}
  .note {{ color:#6B7A8C; font-size:12.5px; margin-top:28px; line-height:1.65; }}
</style></head>
<body>
<div class="page">
<h1>Subscription Recovery Agent — Audit Report</h1>
<div class="sub">Generated directly from logs/audit_log.jsonl. No AI, no server, no external service involved in this page.</div>

<div class="stats">
  <div class="stat"><div class="n">{total}</div><div class="l">Decisions</div></div>
  <div class="stat"><div class="n">{matched}</div><div class="l">LLM matched policy</div></div>
  <div class="stat"><div class="n">{mismatched}</div><div class="l">Gate overrode LLM</div></div>
  <div class="stat"><div class="n">{mismatched/total*100:.0f}%</div><div class="l">Override rate</div></div>
</div>

<h2>Final action distribution (after the gate)</h2>
<div class="panel">{action_bars}</div>

<h2>Volume by decline code</h2>
<div class="panel">{code_bars}</div>

<h2>LLM proposal accuracy by decline code</h2>
<div class="panel">
<input id="filter" placeholder="Filter by decline code...">
<table id="codeTable">
<thead><tr><th>Decline code</th><th>Total</th><th>Matched</th><th>Mismatched</th><th>Match rate</th></tr></thead>
<tbody>
{code_rows}
</tbody>
</table>
</div>

<div class="note">
  "Matched policy" = the LLM's raw proposal, before the gate touched it, was
  identical to the one action config/decline_policy.json assigns that code.
  A low match rate is not a hidden problem - it is exactly what the gate
  exists to catch; see METRICS.md for the full breakdown and the two
  systematic biases behind it.
</div>
</div>

<script>
  document.getElementById('filter').addEventListener('input', function(e) {{
    var q = e.target.value.toLowerCase();
    document.querySelectorAll('#codeTable tbody tr').forEach(function(row) {{
      row.style.display = row.dataset.code.toLowerCase().includes(q) ? '' : 'none';
    }});
  }});
</script>
</body></html>
"""
    REPORT_PATH.write_text(page_html, encoding="utf-8")
    print(f"Wrote {REPORT_PATH} ({total} gate decisions as of this generation)")


if __name__ == "__main__":
    build_report()
