"""
Unit tests for ollama_client.py's failure paths (BUILD_LOG.md §7.3, D4 and
D8). propose_action() is designed to never raise - any Ollama hiccup must
fall back to {"action": None, ...} so agent.py can safely default to the
safest policy action instead of crashing a batch run. These tests mock
requests.post directly so they run in milliseconds with no real Ollama
server needed - CI has no local model to call.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from failsafe import ollama_client


def _fake_response(json_body: dict):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_body
    return resp


def test_no_tool_call_returns_none_action():
    # D4: model responded but didn't call the tool at all.
    with patch(
        "failsafe.ollama_client.requests.post", return_value=_fake_response({"message": {}})
    ):
        result = ollama_client.propose_action(
            {"subscription_id": "s1", "amount_paise": 100, "decline_code": "insufficient_funds"},
            "desc",
            "customer",
        )
    assert result["action"] is None
    assert "no tool call" in result["reasoning"].lower()


def test_malformed_tool_call_arguments_returns_none_action():
    # D4: model called the tool but with unparsable arguments.
    body = {"message": {"tool_calls": [{"function": {"arguments": "{not valid json"}}]}}
    with patch("failsafe.ollama_client.requests.post", return_value=_fake_response(body)):
        result = ollama_client.propose_action(
            {"subscription_id": "s2", "amount_paise": 100, "decline_code": "insufficient_funds"},
            "desc",
            "customer",
        )
    assert result["action"] is None
    assert "malformed" in result["reasoning"].lower()


def test_valid_tool_call_returns_proposed_action():
    body = {
        "message": {
            "tool_calls": [
                {
                    "function": {
                        "arguments": {
                            "action": "delayed_retry",
                            "reasoning": "customer likely low on funds",
                        }
                    }
                }
            ]
        }
    }
    with patch("failsafe.ollama_client.requests.post", return_value=_fake_response(body)):
        result = ollama_client.propose_action(
            {"subscription_id": "s3", "amount_paise": 100, "decline_code": "insufficient_funds"},
            "desc",
            "customer",
        )
    assert result["action"] == "delayed_retry"
    assert result["reasoning"] == "customer likely low on funds"


def test_transient_failure_then_success_retries_and_recovers():
    # D8: one transient failure mid-batch must not sink the record - a
    # retry that then succeeds should return the real proposal, not a
    # failure fallback.
    body = {
        "message": {
            "tool_calls": [
                {"function": {"arguments": {"action": "immediate_retry", "reasoning": "ok"}}}
            ]
        }
    }
    with (
        patch(
            "failsafe.ollama_client.requests.post",
            side_effect=[requests.exceptions.ConnectionError("transient"), _fake_response(body)],
        ) as mock_post,
        patch("failsafe.ollama_client.time.sleep"),
    ):
        result = ollama_client.propose_action(
            {"subscription_id": "s4", "amount_paise": 100, "decline_code": "payment_timed_out"},
            "desc",
            "network",
        )
    assert result["action"] == "immediate_retry"
    assert mock_post.call_count == 2


def test_all_retries_exhausted_falls_back_without_raising():
    # D8: if Ollama is down for the whole retry budget, the caller must
    # still get a clean fallback result, never an exception - a single bad
    # record must never be able to take down a 150-record batch.
    with (
        patch(
            "failsafe.ollama_client.requests.post",
            side_effect=requests.exceptions.ConnectionError("ollama is down"),
        ) as mock_post,
        patch("failsafe.ollama_client.time.sleep"),
    ):
        result = ollama_client.propose_action(
            {"subscription_id": "s5", "amount_paise": 100, "decline_code": "bank_technical_error"},
            "desc",
            "bank",
        )
    assert result["action"] is None
    assert mock_post.call_count == ollama_client.MAX_RETRIES


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    if failures:
        raise SystemExit(1)


# --- healthcheck / async proposal (added 2026-10-31) -----------------------


def test_healthcheck_ok_names_host_and_model():
    from failsafe import ollama_client

    with patch("failsafe.ollama_client.requests.get", return_value=MagicMock(status_code=200)):
        ok, msg = ollama_client.healthcheck()
    assert ok is True
    assert ollama_client.OLLAMA_HOST in msg and ollama_client.OLLAMA_MODEL in msg


def test_healthcheck_non_200_is_unhealthy():
    from failsafe import ollama_client

    with patch("failsafe.ollama_client.requests.get", return_value=MagicMock(status_code=503)):
        ok, msg = ollama_client.healthcheck()
    assert ok is False and "HTTP 503" in msg


def test_healthcheck_connection_error_gives_actionable_hint():
    from failsafe import ollama_client

    with patch("failsafe.ollama_client.requests.get", side_effect=ConnectionError("refused")):
        ok, msg = ollama_client.healthcheck()
    assert ok is False
    assert "refused" in msg and "ollama serve" in msg


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (
            requests.exceptions.ConnectionError(
                "HTTPConnectionPool(host='x', port=9): Max retries exceeded ..."
            ),
            "connection refused or host down",
        ),
        (requests.exceptions.ConnectTimeout("pool timeout dump"), "no answer within 5 s"),
        (requests.exceptions.ReadTimeout("read timeout dump"), "no answer within 5 s"),
        (RuntimeError(), "RuntimeError"),
    ],
)
def test_healthcheck_failure_reason_is_short_and_plain(exc, reason):
    from failsafe import ollama_client

    with patch("failsafe.ollama_client.requests.get", side_effect=exc):
        ok, msg = ollama_client.healthcheck()
    assert ok is False
    assert f"Ollama not reachable at {ollama_client.OLLAMA_HOST}: {reason}." in msg
    assert "Max retries" not in msg and "dump" not in msg
    assert "ollama serve" in msg and "OLLAMA_HOST" in msg


def test_propose_action_async_builds_prompt_and_uses_decision_tool():
    import asyncio

    from failsafe import ollama_client

    caller = MagicMock()

    async def _call_tool(messages, tools, fallback_key, fallback_value):
        caller.seen = (messages, tools, fallback_key, fallback_value)
        return {"action": "delayed_retry", "reasoning": "x"}

    caller.call_tool = _call_tool
    record = {"payment_id": "pay_9", "amount_paise": 12345, "decline_code": "insufficient_funds"}
    with patch("failsafe.ollama_client.OllamaToolCaller", return_value=caller):
        out = asyncio.run(
            ollama_client.propose_action_async(
                record,
                "low balance",
                "customer",
                situation="SITUATION",
                id_field="payment_id",
                record_label="Payment",
            )
        )
    assert out == {"action": "delayed_retry", "reasoning": "x"}
    messages, tools, key, fallback = caller.seen
    prompt = messages[0]["content"]
    assert prompt.startswith("SITUATION")
    assert (
        "Payment: pay_9" in prompt
        and "Rs 123.45" in prompt
        and "Decline source: customer" in prompt
    )
    assert tools == [ollama_client.DECIDE_ACTION_TOOL]
    # A missing/unparseable tool call must fall back to None (-> gate decides), never a guessed action.
    assert (key, fallback) == ("action", None)
