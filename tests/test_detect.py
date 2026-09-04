"""
Unit tests for detect.py's revenue-at-risk DETECTION stage - the "detects
revenue at risk" verb from the track's own problem statement
(PS_REQUIREMENTS_DEBATE.md Round 2, finding 4). Mirrors
tests/test_diagnose.py's mocking style exactly: mock requests.post
directly so these run in milliseconds with no real Ollama server needed -
CI has no local model to call.

detect_at_risk() is designed to never raise, same contract as
diagnose_decline_code()/propose_action(): any Ollama hiccup or malformed
tool call must fall back to {"classification": None, ...} so the caller
(recovery_pipeline.py) can decide how to fail safely instead of crashing a
batch run.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import requests

import detect


def _fake_response(json_body: dict):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_body
    return resp


def test_no_tool_call_returns_none_classification():
    with patch("detect.requests.post", return_value=_fake_response({"message": {}})):
        result = detect.detect_at_risk(
            previous_retry_count=0,
            days_since_last_successful_charge=1,
            most_recent_gateway_response=None,
            subscription_status="active",
        )
    assert result["classification"] is None
    assert "no tool call" in result["reasoning"].lower()


def test_malformed_tool_call_arguments_returns_none_classification():
    body = {"message": {"tool_calls": [{"function": {"arguments": "{not valid json"}}]}}
    with patch("detect.requests.post", return_value=_fake_response(body)):
        result = detect.detect_at_risk(
            previous_retry_count=2,
            days_since_last_successful_charge=10,
            most_recent_gateway_response="Issuer response: do not honor.",
            subscription_status="pending",
        )
    assert result["classification"] is None
    assert "malformed" in result["reasoning"].lower()


def test_missing_classification_field_returns_none():
    body = {"message": {"tool_calls": [{"function": {"arguments": {"reasoning": "no classification key at all"}}}]}}
    with patch("detect.requests.post", return_value=_fake_response(body)):
        result = detect.detect_at_risk(
            previous_retry_count=0,
            days_since_last_successful_charge=1,
            most_recent_gateway_response=None,
            subscription_status="active",
        )
    assert result["classification"] is None
    assert "classification" in result["reasoning"].lower()


def test_correct_classification_of_a_clearly_healthy_record():
    body = {
        "message": {
            "tool_calls": [
                {"function": {"arguments": {
                    "classification": "leave_alone",
                    "reasoning": "No retries, last charge succeeded 1 day ago, status is active.",
                }}}
            ]
        }
    }
    with patch("detect.requests.post", return_value=_fake_response(body)):
        result = detect.detect_at_risk(
            previous_retry_count=0,
            days_since_last_successful_charge=1,
            most_recent_gateway_response=None,
            subscription_status="active",
        )
    assert result["classification"] == "leave_alone"


def test_correct_classification_of_a_clearly_at_risk_record():
    body = {
        "message": {
            "tool_calls": [
                {"function": {"arguments": {
                    "classification": "needs_recovery_attention",
                    "reasoning": "Three prior retries and 20 days since the last successful charge.",
                }}}
            ]
        }
    }
    with patch("detect.requests.post", return_value=_fake_response(body)):
        result = detect.detect_at_risk(
            previous_retry_count=3,
            days_since_last_successful_charge=20,
            most_recent_gateway_response="Bank response: insufficient balance in account.",
            subscription_status="halted",
        )
    assert result["classification"] == "needs_recovery_attention"


def test_model_can_return_a_classification_outside_the_known_enum():
    # Local models don't strictly enforce a JSON-schema enum on tool-call
    # arguments - detect_at_risk() deliberately does not validate against
    # {NEEDS_ATTENTION, LEAVE_ALONE} itself (mirrors diagnose_decline_code(),
    # which likewise never validates against DECLINE_CODES). That
    # validation/fail-safe handling is recovery_pipeline.py's job.
    body = {
        "message": {
            "tool_calls": [
                {"function": {"arguments": {
                    "classification": "not_a_real_classification",
                    "reasoning": "hallucinated",
                }}}
            ]
        }
    }
    with patch("detect.requests.post", return_value=_fake_response(body)):
        result = detect.detect_at_risk(
            previous_retry_count=1,
            days_since_last_successful_charge=2,
            most_recent_gateway_response=None,
            subscription_status="pending",
        )
    assert result["classification"] == "not_a_real_classification"


def test_transient_failure_then_success_retries_and_recovers():
    body = {
        "message": {
            "tool_calls": [{"function": {"arguments": {
                "classification": "needs_recovery_attention", "reasoning": "ok",
            }}}]
        }
    }
    with patch(
        "detect.requests.post",
        side_effect=[requests.exceptions.ConnectionError("transient"), _fake_response(body)],
    ) as mock_post, patch("detect.time.sleep"):
        result = detect.detect_at_risk(
            previous_retry_count=2,
            days_since_last_successful_charge=15,
            most_recent_gateway_response="Issuer response: card blocked.",
            subscription_status="pending",
        )
    assert result["classification"] == "needs_recovery_attention"
    assert mock_post.call_count == 2


def test_all_retries_exhausted_falls_back_without_raising():
    with patch(
        "detect.requests.post",
        side_effect=requests.exceptions.ConnectionError("ollama is down"),
    ) as mock_post, patch("detect.time.sleep"):
        result = detect.detect_at_risk(
            previous_retry_count=0,
            days_since_last_successful_charge=0,
            most_recent_gateway_response=None,
            subscription_status="active",
        )
    assert result["classification"] is None
    assert mock_post.call_count == detect.MAX_RETRIES


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
