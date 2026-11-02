"""Simulate mode is set through an explicit switch, not unittest.mock at runtime."""

from pathlib import Path

import pytest

from failsafe import mcp_server

SRC = Path(__file__).resolve().parent.parent / "src" / "failsafe"


def test_simulate_mode_sets_both_switches_and_restores(monkeypatch):
    monkeypatch.setattr(mcp_server, "SIMULATE", False)
    monkeypatch.setattr(mcp_server._rp, "simulate", False)
    with mcp_server.simulate_mode(True):
        assert mcp_server.SIMULATE is True
        assert mcp_server._rp.simulate is True
    assert mcp_server.SIMULATE is False
    assert mcp_server._rp.simulate is False


def test_simulate_mode_restores_on_error(monkeypatch):
    monkeypatch.setattr(mcp_server, "SIMULATE", False)
    monkeypatch.setattr(mcp_server._rp, "simulate", False)
    with pytest.raises(RuntimeError), mcp_server.simulate_mode(True):
        raise RuntimeError("boom")
    assert (mcp_server.SIMULATE, mcp_server._rp.simulate) == (False, False)


@pytest.mark.parametrize("path", sorted(SRC.glob("*.py")), ids=lambda p: p.name)
def test_no_production_module_imports_unittest_mock(path):
    assert "unittest.mock" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "module", ["checkout_abandonment_agent", "receivables_agent", "integrated_pipeline"]
)
def test_run_exposes_force_simulate_switch(module):
    import importlib
    import inspect

    run = importlib.import_module(f"failsafe.{module}").run
    param = inspect.signature(run).parameters["force_simulate"]
    assert param.default is True
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
