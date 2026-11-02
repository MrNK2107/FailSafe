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

## Replay determinism and what-if (FailSafe)

The same committed logs are the input to the replay engine
([`src/failsafe/replay.py`](src/failsafe/replay.py)), which re-runs every
recorded `gate_decision` through the real `Gate`:

| Check | Command | Result |
|---|---|---|
| Subscriptions reproduce under the committed policy | `python -m failsafe.replay --fail-on-change` | 326/326 decisions identical |
| One-time payments reproduce | `python -m failsafe.replay --audit logs/audit_log_onetime.jsonl --data data/failed_onetime_payments.json --fail-on-change` | 55/55 identical |
| LLM agreement with the committed policy | (same replay) | 320/326 (98%) subscriptions, 48/55 (87%) one-time — the same figures `scripts/metrics.py` reports |
| What-if: `examples/policy_whatif_retry_low_balance.json` | `python -m failsafe.replay --policy examples/policy_whatif_retry_low_balance.json` | 65 decisions change, 59 re-timed, ₹1,190.14 pulled back; LLM agreement drops to 221/326 |

CI enforces the first two rows (`tests.yml` and `policy-check.yml`).

## Override rate of real proposals (latest run per record)

`scripts/metrics.py` counts overrides over all cumulative decisions (6/326
subscriptions, 7/55 one-time). A second, stricter view divides only over
records where the LLM actually produced a valid proposal (`llm_proposed`),
taking the latest published run per record. `python
scripts/corrected_override_metric.py` regenerates
[OVERRIDE_METRIC_CORRECTED.md](OVERRIDE_METRIC_CORRECTED.md):

| Run | Legacy (all records) | Corrected (`override_rate_of_proposals`) |
|---|---|---|
| Subscriptions, latest run | 3/150 (2.0%) | 3/150 (2.0%) |
| One-time payments, latest run | 4/30 (13.3%) | 4/30 (13.3%) |

Every record in those runs got a valid proposal, so the two agree here; they
diverge on runs with parse failures or fallbacks.
