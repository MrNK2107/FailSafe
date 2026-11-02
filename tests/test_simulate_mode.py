"""Simulate mode is set through an explicit switch, not unittest.mock at runtime."""

import asyncio
from pathlib import Path

import pytest

from failsafe import (
    agent,
    integrated_pipeline,
    mcp_server,
    ollama_client,
    razorpay_client,
    real_mcp_demo,
    route_demo,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "failsafe"


def test_simulate_mode_forces_the_single_switch_and_restores(monkeypatch):
    monkeypatch.setattr(razorpay_client, "keys_configured", lambda: True)
    assert mcp_server.is_simulated() is False and mcp_server._rp.simulate is False
    with mcp_server.simulate_mode(True):
        assert razorpay_client.simulate_mode() is True
        assert mcp_server.is_simulated() is True
        assert mcp_server._rp.simulate is True
    assert razorpay_client.simulate_mode() is False
    assert mcp_server._rp.simulate is False


def test_simulate_mode_restores_on_error(monkeypatch):
    monkeypatch.setattr(razorpay_client, "keys_configured", lambda: True)
    with pytest.raises(RuntimeError), mcp_server.simulate_mode(True):
        raise RuntimeError("boom")
    assert (mcp_server.is_simulated(), mcp_server._rp.simulate) == (False, False)


def test_nested_simulate_mode_restores_outer_force():
    with mcp_server.simulate_mode(True):
        with mcp_server.simulate_mode(False):
            pass
        assert razorpay_client._forced[0] is True
    assert razorpay_client._forced[0] is False


def test_mcp_server_holds_no_import_time_snapshot():
    """Regression (merge 275bfe9): mcp_server/razorpay_client once copied
    SIMULATE at import time in three places; nothing may hold a copy now."""
    assert not hasattr(mcp_server, "SIMULATE")
    assert not hasattr(razorpay_client, "SIMULATE")
    assert not hasattr(razorpay_client, "_client")
    assert "simulate" not in vars(mcp_server._rp)  # a property, not a bound value


def test_simulate_mode_false_without_keys_still_simulates_no_attribute_error():
    """Regression: with no keys, `simulate_mode(False)` used to flip _rp into
    real mode with `_client = None`, so create_retry_order raised
    AttributeError. Lifting the force without keys must keep simulating."""
    assert razorpay_client.keys_configured() is False
    with mcp_server.simulate_mode(False):
        out = mcp_server._rp.create_retry_order(5000, "sub_nokeys")
        assert out["simulated"] is True and out["id"].startswith("order_sim_")
        tool_out = asyncio.run(mcp_server.create_retry_order("sub_nokeys_tool", 5000))
        assert tool_out["simulated"] is True


@pytest.mark.parametrize("pipeline", [agent, integrated_pipeline])
def test_health_checks_read_the_live_switch_not_a_snapshot(monkeypatch, pipeline):
    """Regression: agent/integrated_pipeline _check_health read a stale
    razorpay_client.SIMULATE. Flipping keys_configured() after import must
    change whether docker is required."""
    monkeypatch.setattr(ollama_client, "healthcheck", lambda: (True, "ok"))
    calls = []
    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: calls.append(a))
    pipeline._check_health()
    assert calls == []  # no keys -> simulate -> docker not needed
    monkeypatch.setattr(razorpay_client, "keys_configured", lambda: True)
    pipeline._check_health()
    assert len(calls) == 1  # keys now "configured" -> docker checked
    with razorpay_client.force_simulate():
        pipeline._check_health()
    assert len(calls) == 1  # forced simulate -> docker not needed again


def test_real_mcp_demo_and_route_demo_read_the_live_switch(monkeypatch, capsys):
    monkeypatch.setattr(razorpay_client, "keys_configured", lambda: False)
    assert route_demo.main(["--live"]) == 2
    assert "--live needs Razorpay test keys" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="rzp_test_"):
        asyncio.run(real_mcp_demo.run(1))


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
