"""
Unit tests for llm_client.OllamaToolCaller and the module-level healthcheck,
fully offline: requests/httpx are mocked, retry sleeps are patched out.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import requests

from failsafe import llm_client
from failsafe.llm_client import OllamaToolCaller


def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.raise_for_status.return_value = None
    r.json.return_value = payload if payload is not None else {}
    return r


TOOL_PAYLOAD = {
    "message": {
        "tool_calls": [{"function": {"arguments": {"action": "immediate_retry", "reasoning": "r"}}}]
    }
}


# --- module-level healthcheck -------------------------------------------------


def test_healthcheck_ok():
    with patch.object(llm_client.requests, "get", return_value=_resp(200)):
        ok, msg = llm_client.healthcheck()
    assert ok is True
    assert "reachable" in msg


def test_healthcheck_bad_status():
    with patch.object(llm_client.requests, "get", return_value=_resp(503)):
        ok, msg = llm_client.healthcheck()
    assert ok is False
    assert "503" in msg


def test_healthcheck_connection_error_gives_actionable_hint():
    with patch.object(llm_client.requests, "get", side_effect=requests.ConnectionError("refused")):
        ok, msg = llm_client.healthcheck()
    assert ok is False
    assert "ollama serve" in msg


# --- instance healthchecks ----------------------------------------------------


def test_instance_healthcheck_true_and_false():
    c = OllamaToolCaller(host="http://x")
    with patch.object(llm_client.requests, "get", return_value=_resp(200)):
        assert c.healthcheck() is True
    with patch.object(llm_client.requests, "get", side_effect=Exception("boom")):
        assert c.healthcheck() is False


def test_instance_healthcheck_async():
    c = OllamaToolCaller(host="http://x")
    client = MagicMock()
    client.get = AsyncMock(return_value=_resp(200))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    with patch.object(llm_client.httpx, "AsyncClient", return_value=ctx):
        assert asyncio.run(c.healthcheck_async()) is True


# --- _parse_tool_call ---------------------------------------------------------


def test_parse_tool_call_string_arguments_are_decoded():
    c = OllamaToolCaller()
    args, err = c._parse_tool_call(
        {"message": {"tool_calls": [{"function": {"arguments": '{"action": "delayed_retry"}'}}]}}
    )
    assert args == {"action": "delayed_retry"} and err == ""


def test_parse_tool_call_missing_required_key():
    c = OllamaToolCaller()
    args, err = c._parse_tool_call(
        {"message": {"tool_calls": [{"function": {"arguments": {"foo": 1}}}]}}
    )
    assert args is None
    assert "missing 'action'" in err


def test_parse_tool_call_any_key_accepts_anything():
    c = OllamaToolCaller()
    args, _ = c._parse_tool_call(
        {"message": {"tool_calls": [{"function": {"arguments": {"foo": 1}}}]}}, fallback_key="_any"
    )
    assert args == {"foo": 1}


# --- async call_tool ----------------------------------------------------------


def _async_client_returning(*responses_or_errors):
    client = MagicMock()
    client.post = AsyncMock(side_effect=list(responses_or_errors))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, client


def test_call_tool_async_success():
    c = OllamaToolCaller(retries=2)
    ctx, _ = _async_client_returning(_resp(payload=TOOL_PAYLOAD))
    with patch.object(llm_client.httpx, "AsyncClient", return_value=ctx):
        out = asyncio.run(c.call_tool([], []))
    assert out["action"] == "immediate_retry"


def test_call_tool_async_retries_then_falls_back():
    c = OllamaToolCaller(retries=2)
    ctx, client = _async_client_returning(RuntimeError("down"), RuntimeError("still down"))
    with (
        patch.object(llm_client.httpx, "AsyncClient", return_value=ctx),
        patch.object(llm_client.asyncio, "sleep", new=AsyncMock()),
    ):
        out = asyncio.run(c.call_tool([], [], fallback_value="x"))
    assert out["action"] == "x"
    assert "failed after 2 attempts" in out["reasoning"]
    assert client.post.await_count == 2


def test_call_tool_async_no_tool_call_returns_fallback_without_retry():
    c = OllamaToolCaller(retries=3)
    ctx, client = _async_client_returning(_resp(payload={"message": {}}))
    with patch.object(llm_client.httpx, "AsyncClient", return_value=ctx):
        out = asyncio.run(c.call_tool([], []))
    assert out["action"] is None
    assert client.post.await_count == 1


def test_call_tool_falls_back_to_sync_when_httpx_missing():
    c = OllamaToolCaller(retries=1)
    with (
        patch.object(llm_client, "httpx", None),
        patch.object(llm_client.requests, "post", return_value=_resp(payload=TOOL_PAYLOAD)),
    ):
        out = asyncio.run(c.call_tool([], []))
    assert out["action"] == "immediate_retry"


# --- propose_action prompt ----------------------------------------------------


def test_propose_action_builds_prompt_with_record_fields():
    record = {"subscription_id": "sub_p", "amount_paise": 12345, "decline_code": "card_expired"}
    with patch.object(
        llm_client._default_caller, "call_tool_sync", return_value={"action": "payment_link_nudge"}
    ) as m:
        out = llm_client.propose_action(record, "Card expired", "customer")
    assert out == {"action": "payment_link_nudge"}
    prompt = m.call_args[0][0][0]["content"]
    assert "sub_p" in prompt and "Rs 123.45" in prompt and "card_expired" in prompt
