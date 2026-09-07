# GLOSSARY

Plain-language definitions of the terms used across this codebase.

## Pipeline stages

- **Detection** (`failsafe/detect.py`) — deciding *which* records need attention
  at all, from signals a merchant already has (retry counts, days since last
  successful charge, gateway response, subscription status). Output:
  `needs_recovery_attention` or `leave_alone`.
- **Diagnosis** (`failsafe/diagnose.py` and per-domain variants) — inferring
  *why* a payment failed (a decline code / abandonment reason / case reason)
  from the raw bank or gateway message only. Never shown ground truth.
- **Proposal** (`failsafe/ollama_client.py`) — the LLM's suggested *action* for
  the diagnosed problem. Advisory only.
- **Gate** (`failsafe/gate.py` and per-domain variants) — deterministic policy
  enforcement. The LLM proposes; the gate decides. See below.
- **Execution** (`failsafe/mcp_server.py`) — the MCP tools that actually touch
  Razorpay (payment links, retry orders, manual-review flags).

## Safety machinery

- **Policy table** (`config/*.json`) — merchant-editable mapping from decline
  code → allowed action. Loaded and validated at startup; a typo fails loudly.
- **Spending cap** — hard ceilings: ₹50,000 per single action, ₹5,00,000 per
  run. Enforced twice (gate and, independently, inside the MCP tools).
- **Idempotency** — refusing to perform the same (record, action) twice within
  a run.
- **Stopping rules** — cross-run escalation triggers: after
  `MAX_ATTEMPTS_PER_SUBSCRIPTION` (3) real attempts across all runs, or once a
  record is stalled past `STALE_HALT_ESCALATION_DAYS` (12), stop automating and
  hand to a human.
- **Graceful degradation** — any LLM failure (no tool call, malformed output,
  network error) falls back to the *safest* deterministic action, flagged for
  human review; a single model hiccup never crashes a batch.
- **Simulate mode** — with no Razorpay keys set, all Razorpay calls return
  realistic fake responses so the pipeline runs end-to-end with zero accounts.
- **Audit trail** (`logs/*.jsonl`) — append-only JSONL event log. Every
  decision, gate verdict, and tool call gets a line; nothing is overwritten.

## Domain terms

- **Halted subscription** — Razorpay retried a recurring payment 3 times over
  3 days (T+3), all failed, and the subscription is now `halted`. FailSafe's
  flagship domain.
- **One-time payment failure** — a single failed payment; no automatic retry
  cycle exists, so detection and diagnosis matter more.
- **Checkout abandonment** — a customer built a cart and left without paying.
- **Overdue receivable** — a B2B invoice past its due date.

## Infrastructure

- **MCP (Model Context Protocol)** — the protocol between the agent and the
  tools. The agent talks to an in-process MCP server over the real protocol;
  swapping to a subprocess/stdio server is a config change, not a rewrite.
- **Official Razorpay MCP server** — Razorpay's own `mcp/razorpay` docker
  image, used as the executor when real test keys are configured.
- **Ollama** — local LLM runtime. The project's only model dependency; no
  paid APIs anywhere.
