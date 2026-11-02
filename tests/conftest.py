import importlib
import time
from unittest.mock import MagicMock, patch

import pytest

try:
    import requests as _requests

    from failsafe import llm_client as _llm

    for _mod_name in [
        "detect",
        "diagnose",
        "diagnose_checkout_abandonment",
        "diagnose_receivable",
        "ollama_client",
    ]:
        try:
            _m = importlib.import_module(f"failsafe.{_mod_name}")
            if not hasattr(_m, "requests"):
                _m.requests = _requests
            if not hasattr(_m, "time"):
                _m.time = time
        except ImportError:
            pass
    if not hasattr(_llm, "requests"):
        _llm.requests = _requests
except Exception:
    pass


@pytest.fixture
def mock_healthcheck():
    from unittest.mock import patch

    try:
        from failsafe import llm_client, ollama_client

        patches = []
        for mod in [llm_client, ollama_client]:
            if hasattr(mod, "healthcheck"):
                patches.append(patch.object(mod, "healthcheck", return_value=(True, "mocked")))
            else:
                patches.append(
                    patch.object(mod, "healthcheck", return_value=(True, "mocked"), create=True)
                )
        for p in patches:
            p.start()
        yield
        for p in reversed(patches):
            p.stop()
    except Exception:
        yield


@pytest.fixture(autouse=True)
def reset_circuit_breaker():
    """recovery_pipeline's LLM-fallback circuit breaker is module-global;
    without this, tests that inject parse failures trip it and every later
    test in the session silently short-circuits to no_action_unrecoverable."""
    try:
        from failsafe import recovery_pipeline
    except ImportError:
        yield
        return
    recovery_pipeline.reset_circuit_breaker()
    recovery_pipeline.set_proposer(None)
    yield
    recovery_pipeline.reset_circuit_breaker()
    recovery_pipeline.set_proposer(None)


@pytest.fixture(autouse=True)
def reset_mcp_guard():
    try:
        from failsafe import mcp_server

        mcp_server._reset_tool_level_guard_for_tests()
        yield
        mcp_server._reset_tool_level_guard_for_tests()
    except ImportError:
        yield


@pytest.fixture(autouse=True)
def no_real_razorpay_keys(monkeypatch):
    """Hermetic Razorpay state: whatever a developer's local .env holds, every
    test starts with NO keys configured and no simulate force active, and the
    module's key values are restored afterwards (test_razorpay_client.py
    reloads the module with fake keys; without this they would leak into
    every later test). A test that needs "real" mode patches keys_configured
    and stubs the SDK / official MCP call - nothing here can reach Razorpay."""
    try:
        from failsafe import razorpay_client
    except ImportError:
        yield
        return
    monkeypatch.setattr(razorpay_client, "KEY_ID", "")
    monkeypatch.setattr(razorpay_client, "KEY_SECRET", "")
    monkeypatch.setattr(razorpay_client, "_forced", [False])
    yield


@pytest.fixture
def audit_logger(tmp_path):
    from failsafe.audit_log import AuditLogger

    return AuditLogger(tmp_path / "audit.jsonl")


@pytest.fixture
def tmp_audit_logger(tmp_path):
    from failsafe.audit_log import AuditLogger

    return AuditLogger(tmp_path / "nested" / "deep" / "audit.jsonl")


@pytest.fixture
def mock_ollama():
    from failsafe import llm_client

    with patch.object(llm_client.requests, "post") as m, patch.object(llm_client, "time") as _t:
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"message": {}}
        m.return_value = resp
        yield m


@pytest.fixture
def mock_ollama_success():
    from failsafe import llm_client

    with patch.object(llm_client.requests, "post") as m:
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "message": {
                "tool_calls": [
                    {"function": {"arguments": {"action": "delayed_retry", "reasoning": "ok"}}}
                ]
            }
        }
        m.return_value = resp
        yield m
