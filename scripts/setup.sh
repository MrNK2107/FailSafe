#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
echo "=== FailSafe setup (bash) ==="
python3 -m venv .venv || python -m venv .venv
source .venv/bin/activate
pip install -e .
if command -v ollama >/dev/null 2>&1; then
  ollama pull llama3.1:8b || echo "ollama pull failed — run 'ollama serve' first"
else
  echo "Ollama not found — install from https://ollama.com then: ollama pull llama3.1:8b"
fi
python verify_setup.py || true
echo "Done. Run: make demo  or  python -m failsafe.agent --inject-failure diagnosis_parse_failure"
