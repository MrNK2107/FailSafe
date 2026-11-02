# Deploying FailSafe

FailSafe has two parts, and both ship as one container image:

| Process | Command | Needs |
|---|---|---|
| **Console** (default): Decision Trace, audit report, policy view, replay API | `python -m failsafe.console` | the audit log, the policy file |
| **Batch agent**: diagnose → propose → gate → execute → audit | `python -m failsafe.agent` (or `agent_onetime`, `integrated_pipeline`) | an Ollama endpoint; Razorpay **test** keys are optional (SIMULATE without them) |

The console is read-only. It never calls the LLM or Razorpay, and never writes to the audit log. It regenerates its pages into `FAILSAFE_OUT_DIR` whenever the audit log or policy file changes.

## Container

```bash
docker build -t failsafe .

# console on :8080 (serves the committed sample logs)
docker run --rm -p 8080:8080 failsafe

# console over your own logs (mount them read-only)
docker run --rm -p 8080:8080 -v "$PWD/logs:/app/logs:ro" failsafe

# batch agent against an Ollama on the host, test-mode keys from .env
docker run --rm --env-file .env -e OLLAMA_HOST=http://host.docker.internal:11434 \
  -v "$PWD/logs:/app/logs" failsafe python -m failsafe.agent

# offline demo inside the image (no Ollama, no keys)
docker run --rm failsafe python scripts/offline_demo.py --n 12
```

The image is `python:3.12-slim`. It runs as a non-root user (`failsafe`, uid 10001), exposes port 8080, and has a `HEALTHCHECK` on `/healthz`. Ollama is not bundled.

## Environment

| Variable | Used by | Default | Notes |
|---|---|---|---|
| `RAZORPAY_KEY_ID` | agent | empty → SIMULATE | Must be `rzp_test_…`. Never use live keys. |
| `RAZORPAY_KEY_SECRET` | agent | empty | Store it as a secret, not in the image. |
| `RAZORPAY_PARTNER_LINKED_ACCOUNT_ID` | `route_demo` | `acc_sim_partner001` | Route split demo only |
| `OLLAMA_HOST` | agent | `http://localhost:11434` | The agent fails fast if Ollama is unreachable (`--skip-healthcheck` to bypass) |
| `OLLAMA_MODEL` | agent | `llama3.1:8b` | |
| `FAILSAFE_AUDIT_LOG` | console | `logs/audit_log.jsonl` | Any `gate.py`-domain log (subscriptions, one-time) |
| `FAILSAFE_POLICY` | console | `config/decline_policy.json` | Point it at a candidate to serve a what-if |
| `FAILSAFE_DATA` | console | `data/halted_subscriptions.json` | Fills gate inputs that older logs did not record |
| `FAILSAFE_OUT_DIR` | console | a temp dir (`/tmp/failsafe-pages` in the image) | Generated pages; disposable |
| `HOST` | console | `127.0.0.1` (`0.0.0.0` in the image) | |
| `PORT` | console | `8080` | Platforms such as Render and Fly set this |

A template is in [`.env.example`](../.env.example).

## Console routes

| Route | Returns |
|---|---|
| `GET /` (also `/trace`, `/TRACE.html`) | Decision Trace |
| `GET /report` (also `/REPORT.html`) | Audit report |
| `GET /policy` (also `/POLICY_DASHBOARD.html`) | Merchant policy view |
| `GET /policy/edit` | What-if policy editor (in-memory; nothing is saved) |
| `GET /api/policy` | JSON: live policy, available actions, decisions per code |
| `POST /api/whatif` | Body `{"overrides": {"<code>": "<action>"}}` (64 KB max). Returns the replay summary, the PR Markdown and the full candidate file. Unknown codes or actions return 400. |
| `GET /api/replay` | JSON: decisions, reproduced, changed, money moved, LLM agreement, list of changes |
| `GET /api/record/<id>` | Markdown incident report |
| `GET /healthz` | `{"ok": true, ...}`. `ok` is false when the audit log is missing. Pages return 503 in that case. |

## Production checklist

- [ ] **Auth.** The console has none. It shows record ids and amounts. Put it behind your SSO proxy (oauth2-proxy, Cloudflare Access or similar), or keep `HOST=127.0.0.1`.
- [ ] **Keys.** Use test mode only until a human has reviewed `config/decline_policy.json`. Live keys are out of scope for this codebase.
- [ ] **Audit log durability.** `logs/` must be a persistent volume for the agent, because the attempt cap reads the history across runs. The log is append-only and fsynced per event. Back it up, and never edit it.
- [ ] **Policy changes go through a PR.** `.github/workflows/policy-check.yml` replays the committed logs: it fails if the gate's code changes past decisions, and posts a what-if summary for policy changes.
- [ ] **One agent per log.** Checkpoints and the idempotency guard assume a single writer per audit log.
- [ ] **Resources.** The console needs well under 200 MB. Peak test-suite memory measured here is 0.12 GB.

## Platform notes

- **Render / Fly / Railway / Cloud Run:** deploy the Dockerfile as a web service. `PORT` is injected, and the health check path is `/healthz`. Mount a disk at `/app/logs` if the agent runs there too.
- **Cron:** run the agent as a scheduled job (`python -m failsafe.agent`) against the same volume. The console picks up the new log on the next request.
