# Demo Script — 30s + Integrated Pipeline

## 30-Second Flagship Demo (halted subscriptions)

> Prereq: `make setup` or `setup.ps1` once. Needs Ollama running (`ollama serve`).

```bash
python -m failsafe.generate_data
python -m failsafe.agent --inject-failure diagnosis_parse_failure
python -m failsafe.generate_report
# open REPORT.html in browser (double-click, no server needed)
```

What to narrate (30s):

| Second | Action | Say |
|---|---|---|
| 0–5 | `generate_data.py` | "150 synthetic halted subscriptions, 16 decline codes, raw bank messages — no ground-truth leak." |
| 5–15 | `agent.py --inject-failure diagnosis_parse_failure` | "LLM proposes, gate decides. This flag forces the first record's diagnosis to fail — watch it fall back to manual review, not guess. Every other record calls the real model." |
| 15–25 | `generate_report.py` + open `REPORT.html` | "Static, offline report straight from `logs/audit_log.jsonl` — LLM match rate, gate overrides, action distribution. Filter by decline code." |
| 25–30 | Open `POLICY_DASHBOARD.html` | "Merchant view of the same `decline_policy.json` the gate enforces — read-only by design, git is the audit trail." |

Try other inject modes: `llm_parse_failure`, `llm_invalid_action`, `unknown_decline_code`, `repeat_attempts`.

## Integrated Pipeline Demo (all 4 domains)

```bash
python -m failsafe.generate_data
python -m failsafe.generate_data_onetime
python -m failsafe.generate_checkout_abandonment_data
python -m failsafe.generate_receivables_data
python -m failsafe.integrated_pipeline
# writes logs/audit_log_integrated.jsonl — dispatched per-domain, separate audit trails
python -m failsafe.generate_report
python -m failsafe.generate_policy_dashboard
```

Narrate: "One dispatcher, four domains, four gates, four policies — halted subs, one-time payments, checkout abandonment, overdue receivables. Each keeps its own audit trail by design."

## One-liner

```bash
make demo
# or: python verify_setup.py && python -m failsafe.generate_data && python -m failsafe.agent --inject-failure diagnosis_parse_failure && python -m failsafe.generate_report && python -m failsafe.generate_policy_dashboard
```

## Tips for recording

- Keep browser zoom at 100%, light mode.
- `REPORT.html` and `POLICY_DASHBOARD.html` are fully offline — no CDN, safe to record without internet.
- If Ollama is slow, the injected failure still proves the graceful-degradation path instantly.
- Capture screenshots: see `docs/screenshots/README.md`.
