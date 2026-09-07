# DEMO_GUIDE — setup, run order, demo flow, recording plan, presentation script

Everything you need to go from a fresh clone to a recorded, presentable demo.
A status snapshot and the complete/incomplete audit are at the bottom.

---

## 1. One-time setup (≈15 minutes)

### 1.1 Python environment

```bash
python -m venv .venv
source .venv/Scripts/activate      # Windows + Git Bash
# .venv\Scripts\activate           # Windows cmd/PowerShell
pip install -e ".[dev]"            # package + pytest + ruff
```

Verify before recording:

```bash
pytest tests/ -v        # 206 tests, ~2s, no Ollama or keys needed
python -m failsafe --help
```

### 1.2 Fill out `.env`

```bash
copy .env.example .env        # Windows  (macOS/Linux: cp .env.example .env)
```

Open `.env` and fill exactly two lines (leave the Ollama lines as-is unless
you changed the defaults):

| Variable | What to put | Where to get it |
|---|---|---|
| `RAZORPAY_KEY_ID` | `rzp_test_...` from your account | dashboard.razorpay.com → sign up (free, **no KYC**) → toggle **Test Mode** (top-right switch) → **Settings → API Keys → Generate Test Key** |
| `RAZORPAY_KEY_SECRET` | the secret shown once next to it | same screen — copy it now, it is shown only once |
| `OLLAMA_HOST` | `http://localhost:11434` | default; don't touch |
| `OLLAMA_MODEL` | `llama3.1:8b` | default; don't touch |

Rules the code enforces (worth saying on camera):

- Keys **not set** → everything runs in **simulate mode**. The pipeline is
  fully runnable with an empty `.env` — this is a feature, not a fallback.
- A `rzp_live_` key is **refused by construction** (`failsafe/razorpay_client.py`).
- Test mode never touches real money.

### 1.3 Local model (no paid APIs anywhere)

```bash
# Install from https://ollama.com/download (Windows installer), then:
ollama pull llama3.1:8b
ollama run llama3.1:8b "say hi"   # smoke test; Ctrl+D / /bye to exit
```

The Ollama service starts automatically after install on Windows. Verify the
API is up: `curl http://localhost:11434` prints "Ollama is running".

### 1.4 Generate the two UI pages (the new Razorpay navy theme)

```bash
python -m failsafe report       # -> REPORT.html   (from logs/audit_log.jsonl)
python -m failsafe dashboard    # -> POLICY_DASHBOARD.html (from config/decline_policy.json)
```

Both are single offline files — no server, no CDN, safe to demo on flaky wifi.

---

## 2. Fresh-run order (what to run, and when)

### Full pipeline, flagship domain (halted subscriptions)

```bash
python -m failsafe generate-data          # synthesize 150 halted subscriptions
python -m failsafe agent                  # diagnose -> propose -> gate -> execute -> audit
python -m failsafe report                 # refresh REPORT.html
python -m failsafe dashboard              # refresh POLICY_DASHBOARD.html
```

Outputs: `logs/audit_log.jsonl` (append-only audit trail), `logs/results_checkpoint.jsonl`,
`REPORT.html`, `POLICY_DASHBOARD.html`.

### The other three domains (stretch, run if time allows)

```bash
python -m failsafe generate-data-onetime && python -m failsafe agent-onetime
python -m failsafe generate-checkout-abandonment && python -m failsafe checkout-abandonment 30
python -m failsafe generate-receivables && python -m failsafe receivables 30
python -m failsafe integrated 15          # all 4 domains in one dispatch loop
```

### Stage demos (short, camera-friendly)

```bash
python -m failsafe detect-demo            # which records deserve attention
python -m failsafe diagnose-demo          # raw bank message -> decline code
python -m failsafe route-demo             # policy routing table walkthrough
python -m failsafe agent --inject-failure llm_parse_failure   # graceful degradation
python -m failsafe real-mcp-demo 5        # REAL Razorpay test-mode objects (needs real keys)
```

### Numbers straight from the logs

```bash
python scripts/metrics.py                 # recomputes every METRICS.md number
```

---

## 3. The demo flow — every "page" and how the story moves

There is no web app; the demo is a **terminal + two generated HTML pages +
two evidence files**. The narrative arc:

| # | Page / artifact | What it proves | Show for |
|---|---|---|---|
| 1 | Terminal: `python -m failsafe generate-data` | The problem is real data, not a mockup | 15s |
| 2 | Terminal: `python -m failsafe agent` | Diagnose → propose → gate → execute → audit, live | 90s |
| 3 | `REPORT.html` | 150 decisions, 98% LLM-policy match, gate overrides, action mix | 45s |
| 4 | `POLICY_DASHBOARD.html` | The exact policy table the gate enforces, in merchant English, filterable | 45s |
| 5 | Terminal: `agent --inject-failure llm_parse_failure` | When the LLM dies, the system fails *safe* | 30s |
| 6 | `REAL_MCP_RESULTS.md` (+ Razorpay dashboard if you have keys) | 5 real test-mode Razorpay objects with verifiable IDs | 30s |
| 7 | Terminal: `python scripts/metrics.py` | Every reported number is recomputable from committed logs | 15s |

Flow rule: **terminal does the work, HTML does the impressing, markdown does
the proving.** Never scroll raw JSONL on camera — open the report instead.

---

## 4. Recording plan (OBS Studio or Xbox Game Bar `Win+Alt+R`)

### Pre-flight (10 minutes before recording)

- [ ] 1920×1080, browser zoom 110%, terminal font bumped (`Ctrl` + `=` in Windows Terminal)
- [ ] Windows Focus Assist / Do Not Disturb **on**; Slack/email closed
- [ ] Both HTML pages pre-opened in tabs; light browser theme so the navy pages pop
- [ ] **Pre-warm Ollama**: run `python -m failsafe agent` once beforehand — first
      inference loads the model into RAM (30–60s); later runs are fast
- [ ] Pre-generate the flagship data (`generate-data`) so scene 1 is instant
- [ ] Rehearse scene 3→4 tab switch once

### Scene-by-scene shot list (~6 minutes total)

| Scene | Do this on camera | Say this (short form) |
|---|---|---|
| 0 · Title (10s) | Slide or repo README | "FailSafe — recovering the revenue Razorpay leaves behind." |
| 1 · Problem (45s) | README architecture diagram | "Razorpay retries 3× in 3 days, then the subscription halts forever. Nobody chases it. That's the gap." |
| 2 · Data (15s) | `generate-data` | "150 synthetic halted subscriptions, real Razorpay decline messages." |
| 3 · Pipeline (90s) | `python -m failsafe agent` | Narrate one record end-to-end: raw message → diagnosed code → LLM proposal → gate verdict → executed action → audit line. |
| 4 · Report (45s) | `REPORT.html` | "98% of LLM proposals matched policy; the 2% it got wrong, the gate caught — that's the whole point." |
| 5 · Policy (45s) | `POLICY_DASHBOARD.html`, click filters | "This is the literal JSON the gate enforces, in plain English. Read-only on purpose — edits need an audit trail." |
| 6 · Failure (30s) | `--inject-failure llm_parse_failure` | "Kill the LLM mid-run: it degrades to the safest action and flags a human. No crash, no wrong money-move." |
| 7 · Evidence (30s) | `REAL_MCP_RESULTS.md` (or Razorpay dashboard → Orders) | "Five real test-mode objects, IDs you can verify by logging in." |
| 8 · Close (20s) | `python scripts/metrics.py` | "Every number regenerates from committed logs. Cost so far: ₹0." |

### Recording rules

1. Type **short** commands; paste anything long from this file.
2. Never say "um, wait" over a slow model — say "while the model thinks, notice
   the gate already printed its policy lookup" (there is always something on screen).
3. Record in one take per scene; stitch with any editor. Do scene 3 last (it's the risky one).

---

## 5. Presentation script (~5 minutes, word-for-word)

**[0:00 — Hook]**
"Every month, subscription businesses quietly lose money they already earned.
Here's why: when a customer's automatic payment fails, Razorpay retries it
three times over three days. If all three fail, the subscription is marked
*halted* — and then, nothing. No email, no retry, no human. The revenue just
sits there. FailSafe is the agent that picks up exactly where Razorpay stops."

**[0:30 — The idea]**
"It does four things in sequence. *Diagnose*: read the raw, messy bank message
and figure out why the payment failed. *Propose*: ask a local AI model what to
do — retry now, retry later, send a payment link, or hands-off. *Gate*: a
completely ordinary piece of code checks that proposal against a written
policy table and hard spending caps. *Execute*: only gate-approved actions
touch Razorpay, and everything lands in an append-only audit log."

**[1:00 — The golden rule]**
"Here's the one-line design rule: **the LLM proposes, deterministic code
decides.** Models are confident even when they're wrong. One hallucinated
retry on a fraud case is real money. So the AI is an advisor, never the
decision-maker. On our flagship batch of 150 records, the model matched
policy 98% of the time — and the gate overrode it the 2% it didn't. The gate
wins. Every time."

**[1:30 — Live run]**
"Let me show you a real run." *(run `python -m failsafe agent`)*
"Watch one record: this is the raw bank message — *'insufficient funds'* — the
diagnosis stage maps it to a decline code **without ever seeing the answer**.
The local model proposes a delayed retry. The gate checks the policy table:
insufficient funds → delayed retry. Match. The MCP tool executes it and writes
the audit line. That happened 150 times, and there are three stopping rules —
idempotency, a 3-attempt cap across runs, and a 12-day staleness limit — so
the system never nags anyone forever."

**[3:00 — Report page]**
"Everything the run decided is now a page." *(open REPORT.html)*
"Final action distribution after the gate; volume by decline code; and per-code
accuracy with the match rates. All of this is generated straight from the
audit log — no AI, no server. If you don't believe the numbers, the script
that recomputes them is in the repo."

**[3:45 — Policy dashboard]**
"And this is what a merchant sees." *(open POLICY_DASHBOARD.html, click two filters)*
"Every decline code, in plain English, straight from the exact JSON file the
gate enforces — not a copy that can drift. It's deliberately read-only: anything
that changes how money moves needs access control and an edit audit trail,
so edits happen in git, where history already tracks them."

**[4:15 — Graceful degradation]**
"Now the demo I actually enjoy." *(run `--inject-failure llm_parse_failure`)*
"I'm killing the LLM mid-run. Watch: no crash, no wrong action — the pipeline
falls back to the safest possible action and flags it for human review. The
system doesn't need the model to be up; it needs the model to be *bounded*."

**[4:45 — Evidence]**
"One more thing — this isn't only simulation." *(open REAL_MCP_RESULTS.md)*
"Through Razorpay's official MCP server, FailSafe created five **real** objects
in a test-mode account — three retry orders, two payment links — with IDs you
can verify by logging in. Test mode, so still ₹0."

**[5:15 — Close]**
"What's honest about this build: diagnosis accuracy is 82%, not 100 — and the
gate is what makes that safe. Detection is proven but not wired into the
flagship batch. Checkout and receivables run as separate domains. The whole
thing runs on a local model and test-mode keys — total cost, zero rupees.
The LLM proposes. Deterministic code decides. Thank you."

---

## 6. If something goes wrong on camera

| Symptom | Fix / line |
|---|---|
| First inference is slow (30–60s) | You pre-warmed in §4; if not: "model's loading into RAM — one-time cost of running locally." |
| `connection refused` from Ollama | `ollama serve` in a second terminal, or reinstall Ollama. |
| No Razorpay keys | Say "we're in simulate mode — the design runs with zero keys; real keys only add the network round-trip." |
| Gate overrides a lot mid-run | That's the product. "Watch the gate catch it — this is the safety working." |
| Demo machine dies | Both HTML pages are static files on a USB stick; screen-recorded run as backup video. |

---

## 7. Status snapshot (September 2026)

**Complete and committed-clean:**
- All 4 domains (flagship + one-time + checkout abandonment + receivables) with
  their own gates, policies, datasets, and audit logs
- Integrated dispatcher, detection + diagnosis stage demos, real-MCP demo (5
  verifiable test-mode objects), 206-test suite, ruff-clean, CI workflow
- Docs: README, METRICS, BUILD_LOG, GLOSSARY, EASY_EXPLAINER, REAL_MCP_RESULTS
- UI overhaul: REPORT.html + POLICY_DASHBOARD.html now share the Razorpay
  navy/blue dark theme (brand blue `#2B84EB`, semantic action colors), still
  offline single files — regenerate with `python -m failsafe report|dashboard`

**Known gaps (say them before someone finds them):**
- This machine: no `.venv` yet (§1.1) and no `.env` — create before recording
- Detection is not wired into the flagship batch (its dataset has no signal fields)
- The four domains keep separate gates/policies; the dispatcher routes, doesn't merge
- Policy dashboard is read-only by design; no ACL
- Pre-rename audit logs carry `source=recovery-agent` in Razorpay notes
