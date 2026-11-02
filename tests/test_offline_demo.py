"""Smoke test: scripts/offline_demo.py runs the real pipeline with no Ollama/keys
and leaves the committed logs/ and REPORT.html untouched."""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent


def test_offline_demo_runs_and_does_not_touch_repo_artifacts(monkeypatch, capsys):
    before = {
        p: p.read_bytes()
        for p in [REPO / "logs" / "audit_log.jsonl", REPO / "REPORT.html", REPO / "TRACE.html"]
    }
    spec = importlib.util.spec_from_file_location(
        "offline_demo", REPO / "scripts" / "offline_demo.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(sys, "argv", ["offline_demo.py", "--n", "6"])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "gate decisions:            6" in out
    assert "LLM proposals overridden" in out
    assert "6/6 decisions reproduce exactly" in out
    for p, data in before.items():
        assert p.read_bytes() == data
