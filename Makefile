.PHONY: setup verify demo demo-offline report dashboard trace replay whatif test lint typecheck clean

PY ?= python
# The package lives in src/failsafe; make targets work without `pip install -e .`.
export PYTHONPATH := src
VENV ?= .venv
OLLAMA_MODEL ?= llama3.1:8b

setup:
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install -e . || $(VENV)/Scripts/pip install -e .
	-ollama pull $(OLLAMA_MODEL)
	$(VENV)/bin/python verify_setup.py || $(VENV)/Scripts/python verify_setup.py || $(PY) verify_setup.py
	@echo "Setup done. Activate with: source .venv/bin/activate  (bash) or .venv\\Scripts\\Activate.ps1 (PowerShell)"

verify:
	$(PY) verify_setup.py

demo:
	$(PY) -m failsafe.generate_data
	$(PY) -m failsafe.agent --inject-failure diagnosis_parse_failure
	$(PY) -m failsafe.generate_report
	$(PY) -m failsafe.generate_policy_dashboard
	@echo "Open REPORT.html and POLICY_DASHBOARD.html in your browser."

report:
	$(PY) -m failsafe.generate_report
	$(PY) -m failsafe.generate_policy_dashboard
	$(PY) -m failsafe.decision_trace

trace:
	$(PY) -m failsafe.decision_trace

replay:
	$(PY) -m failsafe.replay --fail-on-change

whatif:
	$(PY) -m failsafe.replay --policy examples/policy_whatif_retry_low_balance.json

test:
	$(PY) -m pytest tests/ -v

clean:
	rm -f logs/results_checkpoint.jsonl

demo-offline:
	$(PY) scripts/offline_demo.py --n 20

lint:
	$(PY) -m ruff check src tests scripts

typecheck:
	$(PY) -m mypy src/failsafe
