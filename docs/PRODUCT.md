# FailSafe: product brief

> **The LLM proposes. The gate decides. Every decision replays.**

FailSafe recovers revenue from failed Razorpay payments after Razorpay's own retries have stopped. A local LLM reads the bank's decline message and suggests what to do. A deterministic gate checks that suggestion against the merchant's written policy and hard safety limits before any money moves. Every step is written to an append-only log, and the gate can be re-run on that log at any time to show that it reaches the same decision again.

This brief is derived from what the code in this repository does today. Each claim points to the code or command that backs it. Where the product stops short (synthetic data, test-mode keys, no hosted service), the brief says so.

---

## 1. Who it is for

**Primary: subscription businesses in India billing through Razorpay.** That means SaaS, streaming, edtech and D2C subscription boxes, with anywhere from a few hundred to tens of thousands of recurring mandates. Razorpay retries a failed recurring charge 3 times over 3 days and then marks the subscription `halted` ([BUILD_LOG.md §1](../BUILD_LOG.md)). Whatever is still unpaid at that point is left for the merchant to chase by hand, or it is written off.

| Persona | Job to be done | What FailSafe gives them |
|---|---|---|
| **Head of payments / revenue ops** | "Get halted revenue back without making customers angry or breaking RBI/NPCI norms." | Per-decline-code playbooks (`config/decline_policy.json`), attempt caps, staleness escalation and spend caps, all enforced in code. |
| **Engineering lead** | "I can't ship an LLM that touches money unless I can prove what it will do." | A gate with zero LLM calls (`src/failsafe/gate.py`), 100% test coverage on it, and a replay that re-derives every recorded decision. |
| **Risk / compliance / auditor** | "Show me why this customer got a payment link and that one went to a human." | A Decision Trace page per record: the signal, the diagnosis, the proposal, each gate check and the tool call. The same story is available as a Markdown incident report. |
| **Merchant ops (non-technical)** | "I want to change how we handle expired cards." | A JSON policy with a plain-English glossary, a read-only policy dashboard, and a what-if replay before the change ships. |

**Secondary domains** (same engine, separate gates and policies): one-time payment failures, checkout abandonment and overdue B2B receivables (`src/failsafe/integrated_pipeline.py`).

## 2. The problem

1. **The retry cliff.** After T+3 the processor stops trying. Some of that revenue is still recoverable: a low balance before payday clears with time, and an expired card needs the customer to act once. Some of it is not (fraud flags, blocked instruments). Telling these apart takes reading a messy, free-text bank response.
2. **LLMs can read those responses, but they cannot be trusted with money.** A model that hallucinates `immediate_retry` on a fraud decline, or that follows an injected instruction inside a decline message, causes a compliance incident. In this repo's own runs, the committed audit log shows the model disagreeing with policy on 3 of 150 final decisions, and a deliberately naive stand-in LLM is overridden 10 times in 12 records in the offline demo.
3. **"Why did the bot do that?" usually has no answer.** Agent frameworks log prompts and tokens, not decisions. When a customer complains or an auditor asks, there is no record of which rule fired, and no way to show that the same input would produce the same output today.
4. **Policy changes are made blind.** Changing "insufficient funds → wait" to "retry now" changes outcomes for hundreds of customers. Teams find out what it did after it ships.

## 3. What FailSafe does (shipped)

| Capability | Where | Proof |
|---|---|---|
| Diagnose a decline code from the raw bank message only, never the ground truth | `src/failsafe/diagnose.py` | `diagnosis_matched_ground_truth` is logged per record |
| LLM proposes a typed action (enum, tool-calling) | `src/failsafe/ollama_client.py` | off-enum or empty proposals fall back to the safest action (`llm_invalid_action`, `llm_parse_failure`) |
| **Deterministic gate: 6 ordered checks** (policy table, no-action policy, attempt cap of 3 across runs, staleness of 12+ days, spend caps of ₹50k per action and ₹5L per run, idempotency) | `src/failsafe/gate.py` (`GATE_CHECKS`) | `tests/test_gate.py`, `tests/test_replay.py::test_deciding_check_names_every_gate_branch` |
| Execute through MCP tools with their own tool-level cap; real Razorpay test mode when keys are set, simulated otherwise | `src/failsafe/mcp_server.py` | `REAL_MCP_RESULTS.md` lists verifiable test-mode object ids |
| Append-only, fsynced audit log for every step | `src/failsafe/audit_log.py` | `logs/audit_log.jsonl` (995 events, 11 runs) |
| **Decision Trace console** (headline) | `src/failsafe/decision_trace.py` → `TRACE.html` | [screenshot](screenshots/trace.png) |
| **Deterministic replay, incident reports, policy what-if** (headline) | `src/failsafe/replay.py` | `python -m failsafe.replay` → 326/326 reproduced |
| **Policy check in CI** (headline) | `.github/workflows/policy-check.yml` | a PR that changes the policy gets a job summary of the decisions it would flip |
| **What-if policy editor** (headline) | `src/failsafe/console.py` + `src/failsafe/assets/policy_editor.html` | `/policy/edit`: edit actions per code and see the replay live; download the candidate for a PR ([screenshot](screenshots/policy-editor.png)) |
| **Read-only web console + API** (headline) | `src/failsafe/console.py`, `Dockerfile` | `/`, `/report`, `/policy`, `/api/replay`, `/api/policy`, `POST /api/whatif`, `/api/record/<id>`, `/healthz` |
| Runs fully offline | `scripts/offline_demo.py` | no Ollama, no keys, about 5 s |

## 4. Positioning

**Category:** a decision-safety layer for agentic payments. FailSafe is not a dunning email tool and not a general agent framework.

| Alternative | What it does well | Where FailSafe is different |
|---|---|---|
| **Razorpay's built-in retries / Smart Collect** | Free, zero-setup retries inside the T+3 window. | FailSafe starts where they stop (`halted`), and it chooses the action per decline code instead of retrying blindly. |
| **Dunning SaaS** (Chargebee Retention, Recurly / Stripe Smart Retries, Churnkey and similar) | Mature email/SMS cadences and ML-tuned retry timing, usually on Stripe-first stacks. | India/Razorpay-native decline codes. An explicit, merchant-editable policy instead of a black-box model. Every decision is explainable and replayable. Runs on your own infrastructure at $0 with a local LLM. |
| **Rules engines / BRMS** (Drools, a hand-rolled `if` ladder) | Deterministic and auditable. | They cannot read free-text bank messages. FailSafe uses the LLM for the fuzzy reading and the rules for the decision, and logs both. |
| **Agent frameworks + LLM observability** (LangChain/LangGraph + LangSmith, Langfuse) | Tracing of prompts, tokens and latency. | They trace the model. FailSafe traces the decision: which safety check fired, what money moved, and whether today's gate reproduces it. Replay re-runs the gate rather than the LLM, so it is exact rather than statistical. |
| **Manual ops (a spreadsheet and a human)** | Judgment. | FailSafe sends humans only the cases that need them (stale halts, attempt-capped records, fraud and unrecoverable codes) and gives them the full trace for each. |

**One-line positioning:** *For subscription businesses on Razorpay, FailSafe is the payment-recovery agent you can actually let near money: an LLM reads the decline, a deterministic gate decides, and every decision can be replayed and explained.*

## 5. Differentiators

1. **Replayable decisions.** `python -m failsafe.replay` re-runs every recorded gate decision through today's gate and policy. On the committed logs, 326/326 subscription decisions and 55/55 one-time-payment decisions reproduce exactly (asserted in `tests/test_replay.py::test_committed_audit_logs_reproduce_exactly`). The replay handles logs written before run ids existed by detecting process restarts.
2. **A policy change is a reviewed diff with consequences.** `--policy candidate.json` shows which records flip, the action transitions, the money that moves into or out of automated collection, and how often the recorded LLM proposals would now be overridden. CI posts this on any PR that touches the policy or the gate, and fails if the gate's code silently changes past decisions.
3. **Explainable to a non-engineer.** The Decision Trace shows the six checks in evaluation order, marking which one decided and which were never reached. The same content is available as Markdown for a ticket (`--record <id>` or `/api/record/<id>`).
4. **The LLM never decides.** The gate file has no model calls. Misreads are contained: in the trace for `sub_c02f15ccf66b4e`, the diagnosis misread a card's status and the LLM proposed a payment link, and the gate routed the record to a human under the no-action policy.
5. **$0 and private by default.** It uses a local Ollama model and Razorpay test mode, and it has a SIMULATE mode with no keys. Customer data never leaves the machine.

## 6. Name and brand

- **Name: FailSafe.** The name describes the engineering stance: when anything is uncertain, fail toward a human, never toward money. Product, repository, package and CLI all use it (`failsafe`).
- **Tagline:** *The LLM proposes. The gate decides. Every decision replays.*
- **Mark:** two gate posts with a recovery waveform passing through them ([`docs/brand/logo.svg`](brand/logo.svg), [`wordmark.svg`](brand/wordmark.svg)). The signal is allowed through the gate; it does not go around it.
- **Palette:** ink `#0D1321` (the gate), paper `#F5F4EE` (the ledger), and semantic colours that mean the same thing everywhere:
  - collecting: green `#0B8F63`
  - LLM: violet `#5B3FD0`
  - override / attention: amber `#B45309`
  - the deciding check: orange `#C2410C`
  - to a human: blue `#1D5BD8`
  - blocked: red `#B42318`

  Dark-mode equivalents are defined in `src/failsafe/assets/trace_template.html`.
- **Type:** Inter (falling back to the system UI font) for interface text, and a monospace font for anything that came out of a machine (record ids, codes, actions, tool results).
- **Voice:** plain, specific, and unhyped. Give the number and where it came from, and say "simulated" when it is simulated.

## 7. Business model (hypothesis, not built)

- **Open core.** The engine, gate, replay and trace stay MIT-licensed.
- **Hosted FailSafe:** managed console with SSO, per-merchant policy workspaces, policy approval workflow, scheduled runs against live Razorpay webhooks, and retention of the audit log. Priced per recovered rupee (a success fee of 1–3% of recovered GMV) or per active mandate, so cost tracks value.
- **Wedge:** the replay/what-if gate in CI is useful even to teams that build their own agent, so it can be sold as a standalone "decision CI" for any LLM-in-the-loop money flow.

## 8. What is not true yet

- All data is synthetic (`data/*.json`). Recovery amounts are simulated responses, not settled money. Only 5 real Razorpay test-mode objects are in the committed sample log.
- Detection (`detect.py`) is not wired into the flagship 150-record batch.
- There is no auth on the console. It is read-only and binds to localhost by default; put it behind your own proxy before exposing it.
- "Compliant escalation" means bounded and attempt-capped. It is not integrated with TRAI/DND or with RBI e-mandate pre-debit notifications.
- The checkout and receivables domains have their own gates, which replay does not cover yet. Replay covers `gate.py` (subscriptions and one-time payments).

## 9. Roadmap (next three)

1. Replay for the abandonment and receivables gates, through the same `deciding_check` contract.
2. Let the what-if editor open the PR itself (it already produces the candidate file and the summary). Git stays the approval trail.
3. Webhook ingestion (`subscription.halted`) instead of batch files, with the console tailing the audit log.
