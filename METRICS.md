# METRICS — numbers re-derived from raw audit logs

Every number below is computed from the committed `logs/*.jsonl` audit trails by
[`scripts/metrics.py`](scripts/metrics.py). Regenerate any time:

```bash
python scripts/metrics.py
```

Measurement window: **all committed log content** (multiple runs, cumulative).
Executed money actions are deduped per `(record_id, tool)` — re-runs of the same
record do not double-count value. Definitions are deliberately conservative:
a metric is counted only when the log proves it.

## Halted subscriptions (flagship) — `logs/audit_log.jsonl`

| Metric | Value |
|---|---|
| Records with gate decisions | 150 |
| Gate decisions (all runs, cumulative) | 326 |
| LLM matched policy | 320/326 (98%) |
| Gate overrides of LLM proposal | 6 (2%) |
| Diagnosis matched ground truth | 270/330 (82%) |
| Escalated by stopping rules (attempt cap / staleness) | 74 |
| Executed money actions (unique id × tool) | 108 |
| Total executed value | ₹1,00,138.75 |
| `flag_for_manual_review` calls | 94 |

Reading: the earlier README's "₹41,819.81 recovered / ₹1,50,729.35 at risk /
104 actions" described a *single 150-record run* whose checkpoint was later
extended (the attempt-cap stopping rule added cross-run history). The cumulative
log now shows 326 gate decisions and ₹1,00,138.75 of unique executed value.
Rather than freeze numbers in prose where they silently rot, this file points at
the script; `python scripts/metrics.py` is the source of truth.

## One-time payments — `logs/audit_log_onetime.jsonl`

| Metric | Value |
|---|---|
| Records with gate decisions | 30 |
| Gate decisions | 55 |
| LLM matched policy | 48/55 (87%) |
| Gate overrides | 7 |
| Executed money actions | 29 |
| Total executed value | ₹2,10,111.94 |
| `flag_for_manual_review` calls | 2 |

No escalations: this domain has no halt clock (a one-time payment cannot go
"stale" the way a halted subscription does) and the run predated cross-run
attempt history for its records.

## Checkout abandonment — `logs/audit_log_checkout_abandonment.jsonl`

| Metric | Value |
|---|---|
| Records with gate decisions | 30 unique carts (109 decisions over 3 runs) |
| Diagnosis matched ground truth | 45/109 (41%) |
| Executed money actions | 26 |
| Total executed value | ₹21,261.11 |
| `flag_for_manual_review` calls | 26 |

The 41% diagnosis rate is the honest number, not a flattering one: this domain
diagnoses an abandonment *reason* from a raw session narrative, and 3 runs
accumulated against 12 reason classes. The gate makes a wrong diagnosis cheap
(at worst a mis-targeted nudge or a manual-review flag), which is exactly why
this domain was built diagnosis-driven with no second LLM proposal stage.

## Overdue receivables — `logs/audit_log_receivables.jsonl`

| Metric | Value |
|---|---|
| Records with gate decisions | 30 unique invoices (52 decisions over 2 runs) |
| Diagnosis matched ground truth | 40/52 (77%) |
| Executed money actions | 6 |
| Total executed value | ₹1,88,934.05 |
| `flag_for_manual_review` calls | 39 |

Only 6 executed money actions despite 30 invoices: the ₹50,000 per-action cap
(deliberately shared with the other domains) fires often on B2B invoice sizes,
escalating large invoices to a human instead of auto-nudging — intended
behavior, documented in `failsafe/receivables_gate.py`'s docstring.

## What these numbers do NOT claim

- `simulated_success_rate` in the policy tables is a labeled assumption used by
  the synthetic customer-response simulator, not a measured real-world rate.
- "Recovered" amounts are simulated customer responses to simulated nudges.
  Real Razorpay objects exist only for the 5 test-mode records in
  [REAL_MCP_RESULTS.md](REAL_MCP_RESULTS.md).
- Detection (detect.py) was proven on its 30-record demo log
  (`logs/detection_demo_audit.jsonl`) but is not wired into the flagship batch.
