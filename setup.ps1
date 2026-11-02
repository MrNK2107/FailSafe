Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
Write-Host "=== FailSafe setup (PowerShell) ==="
python -m venv .venv
.venv\Scripts\pip install -e .
try { ollama pull llama3.1:8b } catch { Write-Host "ollama pull failed — run 'ollama serve' first or install from https://ollama.com" }
python verify_setup.py
Write-Host "Done. Run: make demo  or  python -m failsafe.agent --inject-failure diagnosis_parse_failure"
Pop-Location
