# BUILD_LOG

> Provenance note, stated honestly: an earlier BUILD_LOG.md was referenced
> throughout this codebase's docstrings (§1–§18) but was never committed to
> git — the links dangled. This file was (re)written during the September
> 2026 overhaul. What follows is (a) a faithful summary of the project's
> evolution as reconstructed from the committed code, log directories, and
> audit trails, and (b) a detailed record of the overhaul itself. Where the
> original section numbering is cited in docstrings, the claim it supported
> is restated here rather than left dangling.

## Phase 1 — Flagship pipeline (halted subscriptions)

The original build: a gated agent for Razorpay's T+3 problem. Razorpay
retries a failed recurring payment 3 times over 3 days; after that the
subscription is `halted` and nothing further happens automatically. The
pipeline: diagnose (from the raw decline message only) → propose (local LLM)
→ gate (deterministic) → execute (MCP tools) → audit (append-only JSONL).

Key decisions, preserved in code and restated here:

- **The LLM never decides.** Policy lives in `config/decline_policy.json`;
  the gate overrides any off-policy proposal. A gate that depends on the
  model behaving isn't a gate.
- **Diagnosis is blind to ground truth.** `diagnose.py` sees only the raw
  bank/gateway message; the true decline code is logged alongside the
  diagnosed one (`diagnosis_matched_ground_truth`) so accuracy is measured
  honestly but never feeds back into the decision.
- **Graceful degradation is real code, not slides.** Every LLM failure mode
  (no tool call, malformed arguments, invalid enum, network exhaustion after
  3 retries with backoff) falls back to the safest action, flagged for human
  review. `agent.py --inject-failure` replays each failure mode live.
- **Prompt engineering was empirical.** The `record_decision` tool schema in
  `ollama_client.py` carries explicit negative-contrast examples; three
  rewrites were needed to stop the model conflating "bank declined" with
  "bank outage" and "not fraud" with "no_action_fraud". The final config
  hit 98% policy match on the flagship batch.
- **Iteration evidence is preserved**, not deleted: `logs/pre_accuracy_fix/`,
  `logs/pre_diagnosis_reintegration/`, `logs/pre_escalation_rules/`,
  `logs/pre_recording_verification/`, `logs/run3_before_third_fix/`.

## Phase 2 — Honest self-audit and the missing categories

A documented self-audit (the "PS_REQUIREMENTS_DEBATE" cited in docstrings)
found real gaps and closed them in the open:

- **One-time payments** (`agent_onetime.py`) — reused gate, policy, and MCP
  tools unchanged; proved the extraction of `recovery_pipeline.py` (which had
  replaced ~80% duplicated code between the two agents).
- **Checkout abandonment** (`checkout_abandonment_agent.py`) — own gate
  (`abandonment_gate.py`), own policy table, diagnosis-driven: once a reason
  is diagnosed the action is a deterministic lookup, so no second LLM call.
- **Overdue receivables** (`receivables_agent.py` + `receivables_gate.py`) —
  same shape; 4-reminder cap and 90-day legal-review threshold as the
  domain's stopping rules.
- **Detection** (`detect.py`) + **detection pool**
  (`generate_detection_pool.py`) — the pipeline previously processed every
  record unconditionally; detection decides which records deserve attention,
  failing safe toward "needs attention".
- **Compliant-escalation stopping rules** — cross-run attempt cap
  (MAX_ATTEMPTS_PER_SUBSCRIPTION=3, derived from the audit log's history) and
  stale-halt threshold (12 days) in `gate.py`.
- **Defense in depth** — `mcp_server.py` got its own tool-level cap and
  duplicate-call refusal, independent of the gate, so a future caller that
  skips the gate still can't overspend or double-act.
- **Integrated dispatch** (`integrated_pipeline.py`) — one entry point routing
  a mixed record stream to all four domains' real, unmodified pipelines.

Disclosed scope limits (kept as limits, not silently "fixed"): detection is
not wired into the flagship batch (its dataset has no detection signal fields
yet); the four domains keep separate gates and audit logs by design.

## Phase 3 — The overhaul (September 2026)

Full restructure and hygiene pass. Everything below is in this repo's history.

### Packaging

- New `pyproject.toml`: installable package (`pip install -e .`), pinned
  deps, `dev` extras (pytest, ruff), console script entry point.
- All 32 modules moved to `src/failsafe/` as a proper package; every import
  qualified (`from failsafe.gate import Gate`).
- `failsafe/paths.py` — single source of truth for `config/`, `data/`,
  `logs/` locations; replaced ~40 duplicated `Path(__file__).parent.parent`
  computations.
- `python -m failsafe <command>` — one dispatcher for all 16 entry points;
  no more `cd src && python agent.py`.
- Tests import the package directly (`pythonpath` in pytest config); all 26
  `sys.path.insert` hacks deleted. Suite time: ~9s → ~2s.

### Identity

- **Recoup → FailSafe** everywhere: README, docstrings, generated report and
  dashboard titles, MCP server name (`failsafe-recovery-agent`), and the
  Razorpay notes tag (`source=failsafe-agent`). Note for evidence auditing:
  committed audit logs from before the rename carry `source=recovery-agent`.

### Import hygiene

- `razorpay_client.py` rewritten: the SDK client is constructed lazily
  (importing the package builds no network objects), and simulate state is
  ONE dynamic knob (`simulate_mode()`) with a `force_simulate()` context
  manager. The old "two-patch dance" (patch `mcp_server.SIMULATE` *and*
  `_rp.simulate`, or a real API call burns your payment-link quota) is
  structurally impossible — there are no shadow copies left to drift apart.
  9 call sites (5 modules, 4 test files) collapsed to the single seam.

### Repo hygiene

- `.gitignore`: caches, egg-info, build artifacts, generated `RESULTS*.md`
  reports, root-level logs. Committed audit evidence under `logs/` is
  intentionally still tracked.
- Deleted stray `receivables_rerun.log`; fixed `.env.example`.

### Tooling

- `ruff` (lint + format) across `src/` and `tests/`: 116 findings on first
  run (import ordering, unused imports, `assert False` in tests, missing
  exception chaining) — all resolved; both checks now pass clean.
- CI: lint job + test matrix (Python 3.10/3.11/3.12) with pip caching.

### Docs

- This file, `METRICS.md`, `GLOSSARY.md`, `REAL_MCP_RESULTS.md`,
  `EASY_EXPLAINER.md` written against reality: every number derived by
  `scripts/metrics.py` from the committed logs, dead references removed,
  stale docstrings updated to the new simulate seam.
