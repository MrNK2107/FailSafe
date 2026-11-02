# EASY_EXPLAINER — what FailSafe does, in plain language

## The problem

When a customer's automatic (subscription) payment fails, Razorpay retries it
3 times over 3 days. If all 3 fail, the subscription is marked `halted` — and
then nothing happens. Ever. The money just sits there, lost, unless a human
remembers to chase it.

## The idea

Build a robot assistant that picks up exactly where Razorpay stops:

1. **Detect** — Look at a pile of subscriptions and figure out which ones
   actually need help. (Don't waste nudges on healthy customers.)
2. **Diagnose** — Read the raw, messy bank message ("declined by issuer",
   "insufficient funds", "OTP failed"...) and figure out *why* the payment
   failed.
3. **Propose** — Ask a local AI model what to do: retry now? retry later?
   send the customer a payment link? or hands off — this one's fraud?
4. **Gate** — This is the important part. A completely ordinary, boring piece
   of code double-checks the AI's answer against a written policy table and
   hard spending limits. The AI is a *advisor*, not the *decision-maker*. If
   the AI says "retry" on a fraud case, the gate says "no" — and the gate
   wins, every time.
5. **Execute** — Only a gate-approved action actually touches Razorpay, and
   everything is written to an append-only audit log.

## Why not just let the AI decide?

Because models are confident even when they're wrong. One confused answer on
a fraud case, or one hallucinated retry on a ₹60,000 invoice, is real money.
The design rule in one line: **the LLM proposes, deterministic code decides.**

## What stops it from nagging people forever?

Three stopping rules, all plain code:

- **Idempotency** — the same customer never gets the same action twice in
  one run.
- **Attempt cap** — after 3 real recovery attempts across *all* runs, stop
  automating and hand it to a human.
- **Staleness** — a subscription halted 12+ days ago is too cold for
  automated nudges; it goes to a human too.

## Does it actually touch real money?

No. It runs against Razorpay **test mode** (free, no KYC, fake money) with a
local Ollama model (free). With no keys at all, everything runs in simulate
mode. Five real test-mode objects were created during one demo run — their
IDs are in [REAL_MCP_RESULTS.md](REAL_MCP_RESULTS.md) as evidence. The code
refuses to run against a live key, by construction.

## The four domains

| You are... | The robot... |
|---|---|
| A subscription business | recovers halted subscriptions (the flagship) |
| Selling one-off items | diagnoses failed one-time payments |
| An online store | nudges abandoned checkouts |
| A B2B company | chases overdue invoices (politely, and stops at 4 reminders) |

Run all four at once with `python -m failsafe integrated`.

## And if you change the rules?

Every decision the robot ever made is in the audit log, with the inputs the
gate saw. FailSafe can **replay** them: run every past decision through the gate
again. With today's rules, all 326 recorded subscription decisions come out
exactly the same (that is checked on every change). With a *proposed* rule
change, replay shows which customers would have been treated differently and
how much money that moves, before the change goes live:

```bash
python -m failsafe replay --policy examples/policy_whatif_retry_low_balance.json
```

`python -m failsafe console` serves the same thing as web pages: a per-customer
decision trace, the report, the policy, and a what-if editor.

## Cost

₹0. Local model, test-mode API, no paid services anywhere.
