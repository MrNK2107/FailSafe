"""
Unit tests for ollama_client.py's failure paths (BUILD_LOG.md §7.3, D4 and
D8). propose_action() is designed to never raise - any Ollama hiccup must
fall back to {"action": None, ...} so agent.py can safely default to the
safest policy action instead of crashing a batch run. These tests mock
requests.post directly so they run in milliseconds with no real Ollama
server needed - CI has no local model to call.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import requests

import ollama_client


def _fake_response(json_body: dict):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_body
    return resp


def test_no_tool_call_returns_none_action():
    # D4: model responded but didn't call the tool at all.
    with patch("ollama_client.requests.post", return_value=_fake_response({"message": {}})):
        result = ollama_client.propose_action({"subscription_id": "s1", "amount_paise": 100, "decline_code": "insufficient_funds"}, "desc", "customer")
    assert result["action"] is None
    assert "no tool call" in result["reasoning"].lower()


def test_malformed_tool_call_arguments_returns_none_action():
    # D4: model called the tool but with unparsable arguments.
    body = {"message": {"tool_calls": [{"function": {"arguments": "{not valid json"}}]}}
    with patch("ollama_client.requests.post", return_value=_fake_response(body)):
        result = ollama_client.propose_action({"subscription_id": "s2", "amount_paise": 100, "decline_code": "insufficient_funds"}, "desc", "customer")
    assert result["action"] is None
    assert "malformed" in result["reasoning"].lower()


def test_valid_tool_call_returns_proposed_action():
    body = {
        "message": {
            "tool_calls": [
                {"function": {"arguments": {"action": "delayed_retry", "reasoning": "customer likely low on funds"}}}
            ]
        }
    }
    with patch("ollama_client.requests.post", return_value=_fake_response(body)):
        result = ollama_client.propose_action({"subscription_id": "s3", "amount_paise": 100, "decline_code": "insufficient_funds"}, "desc", "customer")
    assert result["action"] == "delayed_retry"
    assert result["reasoning"] == "customer likely low on funds"


def test_transient_failure_then_success_retries_and_recovers():
    # D8: one transient failure mid-batch must not sink the record - a
    # retry that then succeeds should return the real proposal, not a
    # failure fallback.
    body = {"message": {"tool_calls": [{"function": {"arguments": {"action": "immediate_retry", "reasoning": "ok"}}}]}}
    with patch(
        "ollama_client.requests.post",
        side_effect=[requests.exceptions.ConnectionError("transient"), _fake_response(body)],
    ) as mock_post, patch("ollama_client.time.sleep"):
        result = ollama_client.propose_action({"subscription_id": "s4", "amount_paise": 100, "decline_code": "payment_timed_out"}, "desc", "network")
    assert result["action"] == "immediate_retry"
    assert mock_post.call_count == 2


def test_all_retries_exhausted_falls_back_without_raising():
    # D8: if Ollama is down for the whole retry budget, the caller must
    # still get a clean fallback result, never an exception - a single bad
    # record must never be able to take down a 150-record batch.
    with patch(
        "ollama_client.requests.post",
        side_effect=requests.exceptions.ConnectionError("ollama is down"),
    ) as mock_post, patch("ollama_client.time.sleep"):
        result = ollama_client.propose_action({"subscription_id": "s5", "amount_paise": 100, "decline_code": "bank_technical_error"}, "desc", "bank")
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
