# Troubleshooting

## Ollama ConnectionError / `Failed to connect to localhost:11434`

**Symptom:** `requests.exceptions.ConnectionError` or agent hangs on diagnosis/proposal.

**Fix:**
```bash
ollama serve
# in another terminal
ollama pull llama3.1:8b
curl http://localhost:11434/api/tags
```
- Windows: allow Ollama through firewall on first launch.
- Custom host: set `OLLAMA_HOST` in `.env` (e.g. `http://localhost:11434`).
- Still failing: `python verify_setup.py` reports Ollama status. Every agent (`failsafe.agent`, `failsafe.agent_onetime`, `failsafe.checkout_abandonment_agent`, `failsafe.receivables_agent`, `failsafe.integrated_pipeline`) checks Ollama first and exits with `Healthcheck failed: ...` before touching any record. `--skip-healthcheck` bypasses the check, but then each record falls back to policy or manual review, so the results say nothing about the LLM. To see the pipeline with no Ollama at all, run `python scripts/offline_demo.py`.

## `docker not found` / Real Razorpay MCP not reachable

Real Razorpay MCP is optional. Without `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` the pipeline runs in **SIMULATE** mode (`src/failsafe/razorpay_client.py`) — zero setup.
To use real test-mode objects: copy `.env.example` → `.env`, add `rzp_test_` keys from `dashboard.razorpay.com` (Test Mode, no KYC).

## `500` / model reload / Ollama returns malformed JSON

The agent catches per-record exceptions and logs `record_processing_error` — the batch continues. See `BUILD_LOG.md §7.3`.
- Retry the single record: `python -m failsafe.agent --inject-failure llm_parse_failure` exercises the same fallback.
- Check `ollama ps` and `ollama logs` (or restart `ollama serve`).
- Ensure `OLLAMA_MODEL=llama3.1:8b` in `.env`.

## Checkpoint corrupted / `results_checkpoint.jsonl` has bad line

```bash
# inspect
python -c "
import json
for i, l in enumerate(open('logs/results_checkpoint.jsonl', encoding='utf-8'), 1):
    try: l.strip() and json.loads(l)
    except ValueError as e: print('bad line', i, e)
"
# prints nothing if every line parses. If a line is bad, remove it or reset:
rm logs/results_checkpoint.jsonl
python -m failsafe.agent   # re-runs from scratch, audit_log.jsonl is append-only
```
The checkpoint is JSONL — one bad line does not require wiping the whole file: the agents skip lines that do not parse when they resume, so those records are simply processed again.

## Windows path issues (venv, slashes, PowerShell vs bash)

```powershell
# PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1
# if execution policy blocks it:
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -e .

# Bash / Git Bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

# Or just:
make setup        # bash
.\setup.ps1       # PowerShell
```

## `No data found. Run generate_data.py first.`

```bash
python -m failsafe.generate_data
python -m failsafe.generate_data_onetime
python -m failsafe.generate_checkout_abandonment_data
python -m failsafe.generate_receivables_data
```

## `No audit log found` when generating reports

Run the agent first: `python -m failsafe.agent` (or the injected-failure variant). Then `python -m failsafe.generate_report`.

## Port 11434 already in use

Another Ollama is running — reuse it. `curl http://localhost:11434/api/tags` should return 200. Kill duplicates via Task Manager if needed.

## Still stuck?

Run `python verify_setup.py` — it checks Python, deps, Ollama, keys, and data files with actionable hints.
