"""
Unit tests for diagnose.py's root-cause diagnosis stage - the "diagnosing
it" step the problem statement names separately from intervention
selection (PS_REQUIREMENTS_DEBATE.md Round 2). Mirrors
tests/test_ollama_client.py's mocking style exactly: mock
requests.post directly so these run in milliseconds with no real Ollama
server needed - CI has no local model to call.

diagnose_decline_code() is designed to never raise, same contract as
propose_action() - any Ollama hiccup or malformed tool call must fall back
to {"decline_code": None, ...} so the caller (recovery_pipeline.py) can
flag the record for manual review instead of crashing a batch run.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import requests

import diagnose


def _fake_response(json_body: dict):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_body
    return resp


def test_no_tool_call_returns_none_decline_code():
    with patch("diagnose.requests.post", return_value=_fake_response({"message": {}})):
        result = diagnose.diagnose_decline_code("Bank declined this transaction with no further reason provided.")
    assert result["decline_code"] is None
    assert "no tool call" in result["reasoning"].lower()


def test_malformed_tool_call_arguments_returns_none_decline_code():
    body = {"message": {"tool_calls": [{"function": {"arguments": "{not valid json"}}]}}
    with patch("diagnose.requests.post", return_value=_fake_response(body)):
        result = diagnose.diagnose_decline_code("Issuer response: do not honor.")
    assert result["decline_code"] is None
    assert "malformed" in result["reasoning"].lower()


def test_missing_decline_code_field_returns_none():
    body = {"message": {"tool_calls": [{"function": {"arguments": {"reasoning": "no decline_code key at all"}}}]}}
    with patch("diagnose.requests.post", return_value=_fake_response(body)):
        result = diagnose.diagnose_decline_code("Bank response: insufficient balance in account.")
    assert result["decline_code"] is None
    assert "decline_code" in result["reasoning"].lower()


def test_correct_classification_of_an_unambiguous_raw_message():
    # A message from a non-ambiguous template (generate_data.py's
    # RAW_SIGNAL_TEMPLATES) should let the model return the matching code.
    body = {
        "message": {
            "tool_calls": [
                {"function": {"arguments": {
                    "decline_code": "card_expired",
                    "reasoning": "The message explicitly says the card's valid-thru date has passed.",
                }}}
            ]
        }
    }
    with patch("diagnose.requests.post", return_value=_fake_response(body)):
        result = diagnose.diagnose_decline_code("Issuer declined - card expiry date has lapsed.")
    assert result["decline_code"] == "card_expired"
    assert "valid-thru" in result["reasoning"] or "expiry" in result["reasoning"]


def test_ambiguous_raw_message_can_be_misdiagnosed_and_is_passed_through_unvalidated():
    # A generic "do not honor" message is genuinely ambiguous between
    # card_declined, payment_failed, and payment_risk_check_failed
    # (generate_data.py's _GENERIC_BANK_DECLINE_POOL). If the model
    # guesses the wrong one of those three, diagnose_decline_code() must
    # not "correct" it - it passes through exactly what the model said,
    # unvalidated (recovery_pipeline.py is the layer that judges
    # correctness against ground truth, purely for measurement).
    body = {
        "message": {
            "tool_calls": [
                {"function": {"arguments": {
                    "decline_code": "payment_risk_check_failed",
                    "reasoning": "A generic 'do not honor' with no detail often indicates an undisclosed risk hold.",
                }}}
            ]
        }
    }
    with patch("diagnose.requests.post", return_value=_fake_response(body)):
        result = diagnose.diagnose_decline_code("Issuer response: do not honor.")
    # This happens to be "wrong" if the record's true ground truth was
    # card_declined - diagnose.py itself has no opinion on that; it only
    # reports what the model said.
    assert result["decline_code"] == "payment_risk_check_failed"


def test_model_can_return_a_code_outside_the_known_enum():
    # Local models don't strictly enforce a JSON-schema enum on tool-call
    # arguments - diagnose_decline_code() deliberately does not validate
    # against DECLINE_CODES itself (mirrors propose_action(), which
    # likewise never validates `action` against RecoveryAction). That
    # validation is recovery_pipeline.py's job.
    body = {
        "message": {
            "tool_calls": [
                {"function": {"arguments": {
                    "decline_code": "definitely_not_a_real_decline_code",
                    "reasoning": "hallucinated",
                }}}
            ]
        }
    }
    with patch("diagnose.requests.post", return_value=_fake_response(body)):
        result = diagnose.diagnose_decline_code("some raw message")
    assert result["decline_code"] == "definitely_not_a_real_decline_code"


def test_transient_failure_then_success_retries_and_recovers():
    body = {
        "message": {
            "tool_calls": [{"function": {"arguments": {"decline_code": "insufficient_funds", "reasoning": "ok"}}}]
        }
    }
    with patch(
        "diagnose.requests.post",
        side_effect=[requests.exceptions.ConnectionError("transient"), _fake_response(body)],
    ) as mock_post, patch("diagnose.time.sleep"):
        result = diagnose.diagnose_decline_code("Bank response: insufficient balance in account.")
    assert result["decline_code"] == "insufficient_funds"
    assert mock_post.call_count == 2


def test_all_retries_exhausted_falls_back_without_raising():
    with patch(
        "diagnose.requests.post",
        side_effect=requests.exceptions.ConnectionError("ollama is down"),
    ) as mock_post, patch("diagnose.time.sleep"):
        result = diagnose.diagnose_decline_code("Issuing bank's system was unreachable/down.")
    assert result["decline_code"] is None
    assert mock_post.call_count == diagnose.MAX_RETRIES


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
