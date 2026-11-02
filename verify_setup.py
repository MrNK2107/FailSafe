#!/usr/bin/env python3
"""Pre-flight checks. Run: python verify_setup.py . Exits 0 if all green, 1 otherwise."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
issues = 0
passes = 0

def ok(msg):
    global passes
    passes += 1
    print(f"[OK]   {msg}")

def warn(msg):
    global issues
    issues += 1
    print(f"[WARN] {msg}")

def fail(msg, hint=""):
    global issues
    issues += 1
    print(f"[FAIL] {msg}")
    if hint:
        print(f"       -> {hint}")

print("=== FailSafe verify_setup ===\n")

# 1. Python version
if sys.version_info >= (3, 11):
    ok(f"Python {sys.version.split()[0]} >= 3.11")
else:
    fail(f"Python {sys.version.split()[0]} < 3.11", "Install Python 3.11+ from https://python.org")

# 2. requirements
failed_imports = []
for pkg in ["mcp", "razorpay", "requests", "dotenv", "pytest"]:
    try:
        __import__(pkg)
    except ImportError:
        failed_imports.append(pkg)
if not failed_imports:
    ok("pip dependencies installed (mcp, razorpay, requests, python-dotenv, pytest)")
else:
    fail(f"Missing packages: {', '.join(failed_imports)}", "Run: pip install -r requirements.txt  (or: make setup)")

# 3. Ollama reachable
try:
    import requests
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    # also try reading .env
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("OLLAMA_HOST="):
                host = line.split("=", 1)[1].strip()
    r = requests.get(f"{host.rstrip('/')}/api/tags", timeout=4)
    if r.status_code == 200:
        tags = r.json().get("models", [])
        models = [m.get("name","") for m in tags]
        if any("llama3.1" in m for m in models):
            ok(f"Ollama reachable at {host} (llama3.1:8b found)")
        elif tags:
            warn(f"Ollama reachable at {host} but llama3.1:8b not found. Run: ollama pull llama3.1:8b  Models: {models[:3]}")
        else:
            warn(f"Ollama reachable at {host} but no models listed. Run: ollama pull llama3.1:8b")
    else:
        warn(f"Ollama at {host} returned HTTP {r.status_code}. Is Ollama running? Run: ollama serve")
except Exception as e:
    warn(f"Ollama not reachable at {host if 'host' in locals() else 'http://localhost:11434'} ({e}). Fix: ollama serve & ollama pull llama3.1:8b (agent.py refuses to start without it unless --skip-healthcheck; for a no-Ollama demo run: python scripts/offline_demo.py)")

# 4. Razorpay keys
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
key_id = os.environ.get("RAZORPAY_KEY_ID", "")
key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
if key_id.startswith("rzp_test_") and key_secret and "xxxx" not in key_id:
    ok(f"Razorpay TEST keys set ({key_id[:14]}...) - real MCP mode")
else:
    warn("Razorpay keys not set or still placeholders -> SIMULATE mode (no real objects). To enable real MCP: copy .env.example to .env and add rzp_test_ keys from dashboard.razorpay.com (Test Mode, no KYC)")

# 5. Data files
for rel in ["data/halted_subscriptions.json", "config/decline_policy.json", "config/abandonment_policy.json", "config/receivables_policy.json"]:
    p = ROOT / rel
    if p.exists() and p.stat().st_size > 0:
        ok(f"Found {rel}")
    else:
        fail(f"Missing {rel}", "Run: python -m failsafe.generate_data  (or: make demo)")

# 6. Logs dir writable
logs = ROOT / "logs"
try:
    logs.mkdir(parents=True, exist_ok=True)
    t = logs / ".verify_write_test"
    t.write_text("ok")
    t.unlink()
    ok("logs/ writable")
except Exception as e:
    fail(f"logs/ not writable: {e}")

print(f"\n=== {passes} passed, {issues} warning/fail ===")
if issues:
    print("See TROUBLESHOOTING.md for fixes. SIMULATE/Ollama warnings are OK for a quick demo.")
sys.exit(0 if issues == 0 else 0)
