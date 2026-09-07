"""
Generates POLICY_DASHBOARD.html: a single, self-contained, offline static
page rendering config/decline_policy.json for a merchant - no server, no
framework, no CDN dependency, same architecture decision as
generate_report.py (BUILD_LOG.md §8, README.md §5): this project's static
pages must never depend on the internet being up during a demo.

This is a READ-ONLY view, on purpose. It exists to close a real gap found
during review: a merchant editing config/decline_policy.json by hand had no
way to know what an allowed_action like "payment_link_nudge" actually does
without opening a .py file - the opposite of the "no code, no redeploy"
pitch. This page answers that in plain English, with filters, straight from
the same file the gate actually enforces - nothing here is a separate copy
that could drift out of sync.

It is deliberately NOT an editable dashboard - see README.md's Known
Limitations for exactly why (no access control, no audit trail on policy
changes, matching real security concerns Razorpay's own docs raise for
anything that can change how money moves). Editing the policy still means
editing config/decline_policy.json directly, whose own git history is the
audit trail for who changed what and when.

Theme: Razorpay-brand navy/blue dark console, the same palette and panel
language as generate_report.py so both pages read as one product on camera.

Usage: python generate_policy_dashboard.py
"""

import html
import json

from failsafe.paths import CONFIG_DIR, PROJECT_ROOT

POLICY_PATH = CONFIG_DIR / "decline_policy.json"
DASHBOARD_PATH = PROJECT_ROOT / "POLICY_DASHBOARD.html"

SOURCE_COLORS = {
    "customer": "#2B84EB",
    "bank": "#F5A623",
    "gateway": "#9B8AFB",
    "network": "#00C481",
}

ACTION_COLORS = {
    "immediate_retry": "#00C481",
    "delayed_retry": "#2B84EB",
    "payment_link_nudge": "#F5A623",
    "no_action_fraud": "#ED5E68",
    "no_action_unrecoverable": "#7E93B4",
}

ACTION_LABELS = {
    "immediate_retry": "Immediate retry",
    "delayed_retry": "Delayed retry",
    "payment_link_nudge": "Payment link nudge",
    "no_action_fraud": "No action — fraud",
    "no_action_unrecoverable": "No action — unrecoverable",
}


def _load_policy() -> tuple[dict, dict]:
    if not POLICY_PATH.exists():
        raise SystemExit(f"No policy file found at {POLICY_PATH}.")
    raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    glossary = raw.get("_action_glossary", {})
    codes = {k: v for k, v in raw.items() if not k.startswith("_")}
    return codes, glossary


def _bar(label: str, value: int, max_value: int, color: str) -> str:
    pct = (value / max_value * 100) if max_value else 0
    safe_label = html.escape(str(label))
    return (
        f'<div class="bar-row">'
        f'<span class="bar-label">{safe_label}</span>'
        f'<div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%;background:{color}"></div></div>'
        f'<span class="bar-value">{value}</span>'
        f"</div>"
    )


def _donut(counts: dict[str, int], colors: dict[str, str], total: int) -> str:
    # Pure-CSS conic-gradient donut - no chart library, no CDN, works
    # completely offline. Segments in a fixed, deterministic order so the
    # legend below always lines up with what's drawn.
    stops = []
    angle = 0.0
    for key, count in counts.items():
        if count == 0:
            continue
        span = count / total * 360
        color = colors.get(key, "#7E93B4")
        stops.append(f"{color} {angle:.2f}deg {angle + span:.2f}deg")
        angle += span
    gradient = ", ".join(stops)
    return f'<div class="donut" style="background:conic-gradient({gradient})"></div>'


def build_dashboard():
    codes, glossary = _load_policy()
    if not codes:
        raise SystemExit(f"{POLICY_PATH} has no decline codes to render.")

    total = len(codes)

    by_source: dict[str, int] = {}
    by_action: dict[str, int] = {}
    for entry in codes.values():
        by_source[entry["source"]] = by_source.get(entry["source"], 0) + 1
        by_action[entry["allowed_action"]] = by_action.get(entry["allowed_action"], 0) + 1

    # Fixed action order everywhere (cards, donut, legend, filter pills) so
    # colors and labels always mean the same thing across the whole page.
    action_order = [a for a in ACTION_LABELS if a in by_action] + [
        a for a in by_action if a not in ACTION_LABELS
    ]
    ordered_by_action = {a: by_action[a] for a in action_order}

    max_source = max(by_source.values())
    source_bars = "".join(
        _bar(source.capitalize(), n, max_source, SOURCE_COLORS.get(source, "#7E93B4"))
        for source, n in sorted(by_source.items(), key=lambda kv: -kv[1])
    )

    donut_html = _donut(ordered_by_action, ACTION_COLORS, total)
    legend_html = "".join(
        f'<div class="legend-row"><span class="swatch" style="background:{ACTION_COLORS.get(a, "#7E93B4")}"></span>'
        f'<span>{html.escape(ACTION_LABELS.get(a, a))}</span><span class="legend-count">{n}</span></div>'
        for a, n in ordered_by_action.items()
    )

    cards_html = ""
    for code, entry in sorted(codes.items()):
        source = entry["source"]
        action = entry["allowed_action"]
        desc = html.escape(entry["description"])
        explanation = html.escape(glossary.get(action, "(no explanation on file for this action)"))
        action_label = html.escape(ACTION_LABELS.get(action, action))
        action_color = ACTION_COLORS.get(action, "#7E93B4")
        source_color = SOURCE_COLORS.get(source, "#7E93B4")
        rate = entry.get("simulated_success_rate", 0.0)
        safe_code = html.escape(code)
        cards_html += f"""
<div class="card" data-code="{safe_code}" data-source="{html.escape(source)}" data-action="{html.escape(action)}"
     data-search="{safe_code} {desc}">
  <div class="card-top">
    <code class="code-name">{safe_code}</code>
    <span class="pill" style="background:{source_color}26;color:{source_color}">{html.escape(source)}</span>
  </div>
  <div class="desc">{desc}</div>
  <div class="action-row">
    <span class="pill" style="background:{action_color}26;color:{action_color}">{action_label}</span>
  </div>
  <div class="explain">{explanation}</div>
  <div class="sim-note">Simulated success rate: {rate * 100:.0f}% <span class="sim-caveat">(test-data only, not a real guarantee — see note below)</span></div>
</div>"""

    source_pills = "".join(
        f'<button class="pill-btn" data-filter-source="{html.escape(s)}">{html.escape(s.capitalize())}</button>'
        for s in sorted(by_source)
    )
    action_pills = "".join(
        f'<button class="pill-btn" data-filter-action="{html.escape(a)}">{html.escape(ACTION_LABELS.get(a, a))}</button>'
        for a in action_order
    )

    page_html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>FailSafe Policy - Merchant View</title>
<style>
  * {{ box-sizing:border-box; }}
  ::selection {{ background:#2B84EB; color:#FFFFFF; }}
  body {{
    font: 14px/1.6 -apple-system, "Segoe UI", Roboto, sans-serif;
    background:#061C3F;
    background-image:
      radial-gradient(1100px 460px at 12% -8%, rgba(43,132,235,.30), transparent 60%),
      radial-gradient(900px 420px at 96% 0%, rgba(90,169,245,.16), transparent 55%);
    background-repeat:no-repeat;
    color:#F2F7FD; margin:0; padding:56px 64px; min-height:100vh;
  }}
  .page {{ max-width:1080px; margin:0 auto; }}

  .brand {{ display:flex; align-items:center; gap:12px; margin-bottom:8px; }}
  .logo {{ width:36px; height:36px; border-radius:10px; flex-shrink:0;
           background:linear-gradient(135deg,#2B84EB,#5AA9F5);
           display:flex; align-items:center; justify-content:center;
           font-weight:800; font-size:18px; color:#FFFFFF;
           box-shadow:0 6px 20px rgba(43,132,235,.45); }}
  h1 {{ font-size:23px; font-weight:700; color:#FFFFFF; margin:0; letter-spacing:-.015em; }}
  h1 .accent {{ color:#5AA9F5; }}
  .badge {{ margin-left:auto; font-size:10.5px; font-weight:700; letter-spacing:.08em;
            text-transform:uppercase; color:#5AA9F5; border:1px solid rgba(90,169,245,.45);
            border-radius:99px; padding:5px 12px; background:rgba(43,132,235,.12); white-space:nowrap; }}
  .sub {{ color:#9DB8DC; margin-bottom:38px; max-width:760px; font-size:13px; }}
  .sub code {{ color:#9DC1EA; }}

  h2 {{ font-size:12.5px; font-weight:700; color:#7FA6D9; text-transform:uppercase; letter-spacing:.08em;
        margin:0 0 16px; display:flex; align-items:center; gap:9px; }}
  h2::before {{ content:""; width:14px; height:3px; border-radius:2px;
                background:linear-gradient(90deg,#2B84EB,#5AA9F5); }}

  .stats {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:14px; margin-bottom:46px; }}
  .stat {{ background:linear-gradient(180deg,#0D2F5F,#0A2549); border:1px solid rgba(90,169,245,.22);
           border-radius:14px; padding:20px 22px; box-shadow:0 14px 34px -22px rgba(2,10,26,.95); }}
  .stat .n {{ font-size:28px; font-weight:700; color:#FFFFFF; font-variant-numeric:tabular-nums; }}
  .stat .l {{ color:#9DB8DC; font-size:11px; text-transform:uppercase; letter-spacing:.07em; margin-top:4px; }}

  .panel {{ background:linear-gradient(180deg,#0C2C5A,#0A2549); border:1px solid rgba(90,169,245,.18);
            border-radius:14px; padding:22px 24px; box-shadow:0 14px 34px -22px rgba(2,10,26,.95); }}
  .charts {{ display:flex; gap:20px; flex-wrap:wrap; align-items:stretch; margin-bottom:48px; }}
  .chart-block {{ flex:1; min-width:300px; }}
  .bar-row {{ display:flex; align-items:center; gap:12px; margin:11px 0; }}
  .bar-row:first-of-type {{ margin-top:0; }}
  .bar-label {{ width:100px; flex-shrink:0; color:#C9DCF3; font-size:13px; }}
  .bar-track {{ flex:1; background:rgba(255,255,255,.07); border-radius:4px; height:8px; overflow:hidden; }}
  .bar-fill {{ height:100%; border-radius:4px; box-shadow:0 0 10px rgba(43,132,235,.25); }}
  .bar-value {{ width:26px; text-align:right; color:#9DB8DC; font-size:12px; font-weight:700; font-variant-numeric:tabular-nums; }}

  .donut-wrap {{ display:flex; gap:28px; align-items:center; flex-wrap:wrap; }}
  .donut {{ width:118px; height:118px; border-radius:50%; flex-shrink:0;
            mask: radial-gradient(farthest-side, transparent calc(100% - 30px), #000 calc(100% - 30px));
            -webkit-mask: radial-gradient(farthest-side, transparent calc(100% - 30px), #000 calc(100% - 30px)); }}
  .legend-row {{ display:flex; align-items:center; gap:8px; font-size:13px; margin:7px 0; color:#C9DCF3; }}
  .swatch {{ width:10px; height:10px; border-radius:3px; flex-shrink:0; }}
  .legend-count {{ color:#9DB8DC; margin-left:auto; padding-left:18px; font-weight:700; font-variant-numeric:tabular-nums; }}

  .controls {{ display:flex; gap:28px; flex-wrap:wrap; align-items:flex-start; margin:8px 0 20px; }}
  input#search {{ background:rgba(255,255,255,.06); border:1px solid rgba(90,169,245,.35); color:#F2F7FD;
                  padding:9px 13px; border-radius:8px; width:280px; font-size:13px; }}
  input#search::placeholder {{ color:#6E8BB3; }}
  input#search:focus {{ outline:none; border-color:#2B84EB; background:rgba(255,255,255,.09);
                        box-shadow:0 0 0 3px rgba(43,132,235,.25); }}
  .filter-group {{ display:flex; flex-direction:column; gap:7px; }}
  .filter-group .label {{ color:#9DB8DC; font-size:11px; text-transform:uppercase; letter-spacing:.06em; }}
  .pill-row {{ display:flex; gap:6px; flex-wrap:wrap; }}
  .pill-btn {{ background:rgba(255,255,255,.05); border:1px solid rgba(90,169,245,.3); color:#C9DCF3;
               padding:5px 13px; border-radius:99px; font-size:12px; cursor:pointer;
               transition:border-color .12s, color .12s, background .12s; }}
  .pill-btn:hover {{ border-color:#2B84EB; color:#FFFFFF; }}
  .pill-btn.active {{ background:#2B84EB; border-color:#2B84EB; color:#FFFFFF; }}

  .grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:16px; margin-top:8px; }}
  .card {{ background:linear-gradient(180deg,#0C2C5A,#0A2549); border:1px solid rgba(90,169,245,.18);
           border-radius:14px; padding:18px 20px; box-shadow:0 14px 34px -22px rgba(2,10,26,.95);
           transition:box-shadow .15s, transform .15s, border-color .15s; }}
  .card:hover {{ border-color:rgba(90,169,245,.5); box-shadow:0 18px 40px -20px rgba(43,132,235,.35);
                 transform:translateY(-2px); }}
  .card-top {{ display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:10px; }}
  .code-name {{ font-size:13px; color:#FFFFFF; font-weight:700; font-family:ui-monospace, "Cascadia Code", Consolas, monospace; }}
  .desc {{ color:#C9DCF3; font-size:13px; margin-bottom:12px; min-height:36px; }}
  .action-row {{ margin-bottom:10px; }}
  .pill {{ padding:3px 10px; border-radius:99px; font-weight:700; font-size:11.5px; white-space:nowrap; }}
  .explain {{ color:#9DB8DC; font-size:12.5px; margin-bottom:12px; }}
  .sim-note {{ color:#6E8BB3; font-size:11px; border-top:1px solid rgba(157,184,220,.12); padding-top:10px;
               font-variant-numeric:tabular-nums; }}
  .sim-caveat {{ font-style:italic; }}
  .empty {{ color:#9DB8DC; padding:24px; text-align:center; display:none; }}
  .note {{ color:#7FA0C8; font-size:12.5px; margin-top:40px; max-width:760px; line-height:1.65; }}
  .note b {{ color:#FFFFFF; }}
  .note code {{ color:#9DC1EA; }}
</style></head>
<body>
<div class="page">
<div class="brand">
  <div class="logo">₹</div>
  <h1>Fail<span class="accent">Safe</span> Policy — Merchant View</h1>
  <span class="badge">Read-only by design</span>
</div>
<div class="sub">Generated directly from <code>config/decline_policy.json</code> — the exact live policy the gate
enforces, not a mockup or a separate copy. Read-only by design; see the note at the bottom for why.</div>

<div class="stats">
  <div class="stat"><div class="n">{total}</div><div class="l">Decline codes covered</div></div>
  <div class="stat"><div class="n">{len(by_source)}</div><div class="l">Failure sources</div></div>
  <div class="stat"><div class="n">{len(by_action)}</div><div class="l">Distinct actions used</div></div>
</div>

<div class="charts">
  <div class="chart-block panel">
    <h2>By who/what caused the failure</h2>
    {source_bars}
  </div>
  <div class="chart-block panel">
    <h2>By what the system does about it</h2>
    <div class="donut-wrap">
      {donut_html}
      <div>{legend_html}</div>
    </div>
  </div>
</div>

<h2>Every decline code, in plain English</h2>
<div class="controls">
  <div class="filter-group">
    <span class="label">Search</span>
    <input id="search" placeholder="Search code or description...">
  </div>
  <div class="filter-group">
    <span class="label">Source</span>
    <div class="pill-row" id="sourceFilters">
      <button class="pill-btn active" data-filter-source="all">All</button>
      {source_pills}
    </div>
  </div>
  <div class="filter-group">
    <span class="label">Action</span>
    <div class="pill-row" id="actionFilters">
      <button class="pill-btn active" data-filter-action="all">All</button>
      {action_pills}
    </div>
  </div>
</div>

<div class="grid" id="cardGrid">
{cards_html}
</div>
<div class="empty" id="emptyState">No decline codes match these filters.</div>

<div class="note">
  <b>Why this page is read-only, not an editable dashboard:</b> the policy below decides how real money-moving
  actions get triggered, so letting anyone change it through a web form needs real access control (who's allowed
  to edit) and an audit trail on the edit itself (who changed what, when) — neither of which this project has
  built. Changing the actual policy still means editing <code>config/decline_policy.json</code> directly; that
  file's own git history already serves as the audit trail for who changed what and when. See README.md's Known
  Limitations for the full reasoning.
</div>
</div>

<script>
  var search = document.getElementById('search');
  var sourceFilters = document.getElementById('sourceFilters');
  var actionFilters = document.getElementById('actionFilters');
  var cards = Array.prototype.slice.call(document.querySelectorAll('#cardGrid .card'));
  var empty = document.getElementById('emptyState');

  var state = {{ q: '', source: 'all', action: 'all' }};

  function applyFilters() {{
    var visible = 0;
    cards.forEach(function(card) {{
      var matchesSearch = !state.q || card.dataset.search.toLowerCase().includes(state.q);
      var matchesSource = state.source === 'all' || card.dataset.source === state.source;
      var matchesAction = state.action === 'all' || card.dataset.action === state.action;
      var show = matchesSearch && matchesSource && matchesAction;
      card.style.display = show ? '' : 'none';
      if (show) visible++;
    }});
    empty.style.display = visible === 0 ? 'block' : 'none';
  }}

  search.addEventListener('input', function(e) {{
    state.q = e.target.value.toLowerCase();
    applyFilters();
  }});

  function wirePillGroup(container, stateKey, datasetKey) {{
    container.addEventListener('click', function(e) {{
      var btn = e.target.closest('.pill-btn');
      if (!btn) return;
      container.querySelectorAll('.pill-btn').forEach(function(b) {{ b.classList.remove('active'); }});
      btn.classList.add('active');
      state[stateKey] = btn.dataset[datasetKey];
      applyFilters();
    }});
  }}

  wirePillGroup(sourceFilters, 'source', 'filterSource');
  wirePillGroup(actionFilters, 'action', 'filterAction');
</script>
</body></html>
"""
    DASHBOARD_PATH.write_text(page_html, encoding="utf-8")
    print(f"Wrote {DASHBOARD_PATH} ({total} decline codes)")


if __name__ == "__main__":
    build_dashboard()
