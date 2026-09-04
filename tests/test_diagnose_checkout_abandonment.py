"""
Unit tests for diagnose_checkout_abandonment.py - mirrors
tests/test_diagnose.py's mocking style exactly: mock requests.post
directly so these run in milliseconds with no real Ollama server needed.

diagnose_abandonment_reason() is designed to never raise, same contract
as diagnose_decline_code()/propose_action() - any Ollama hiccup or
malformed tool call must fall back to {"reason": None, ...} so the caller
(checkout_abandonment_agent.py) can flag the record for manual review
instead of crashing a batch run.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import requests

import diagnose_checkout_abandonment as diag


def _fake_response(json_body: dict):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_body
    return resp


def _call(**overrides):
    kwargs = dict(
        checkout_stage="card_details_entry",
        minutes_since_abandonment=5,
        device_type="mobile_web",
        is_returning_customer=True,
        amount_paise=29900,
    )
    kwargs.update(overrides)
    return diag.diagnose_abandonment_reason(**kwargs)


def test_no_tool_call_returns_none_reason():
    with patch("diagnose_checkout_abandonment.requests.post", return_value=_fake_response({"message": {}})):
        result = _call()
    assert result["reason"] is None
    assert "no tool call" in result["reasoning"].lower()


def test_malformed_tool_call_arguments_returns_none_reason():
    body = {"message": {"tool_calls": [{"function": {"arguments": "{not valid json"}}]}}
    with patch("diagnose_checkout_abandonment.requests.post", return_value=_fake_response(body)):
        result = _call()
    assert result["reason"] is None
    assert "malformed" in result["reasoning"].lower()


def test_missing_reason_field_returns_none():
    body = {"message": {"tool_calls": [{"function": {"arguments": {"reasoning": "no reason key at all"}}}]}}
    with patch("diagnose_checkout_abandonment.requests.post", return_value=_fake_response(body)):
        result = _call()
    assert result["reason"] is None
    assert "reason" in result["reasoning"].lower()


def test_correct_classification_of_an_unambiguous_case():
    body = {
        "message": {
            "tool_calls": [
                {"function": {"arguments": {
                    "reason": "price_shock",
                    "reasoning": "Abandoned at final review with a large cart - classic price sensitivity.",
                }}}
            ]
        }
    }
    with patch("diagnose_checkout_abandonment.requests.post", return_value=_fake_response(body)):
        result = _call(checkout_stage="review_confirm", minutes_since_abandonment=4, amount_paise=899900)
    assert result["reason"] == "price_shock"
    assert "review" in result["reasoning"] or "price" in result["reasoning"]


def test_ambiguous_case_can_be_misdiagnosed_and_is_passed_through_unvalidated():
    # otp_entry + 10 minutes is the deliberately-ambiguous cluster shared
    # by otp_delay_or_failure and distraction_or_multitasking
    # (generate_checkout_abandonment_data.py). If the model guesses the
    # wrong one, diagnose_abandonment_reason() must not "correct" it - it
    # passes through exactly what the model said.
    body = {
        "message": {
            "tool_calls": [
                {"function": {"arguments": {
                    "reason": "distraction_or_multitasking",
                    "reasoning": "A 10-minute gap at OTP entry could just as easily be the customer getting pulled away.",
                }}}
            ]
        }
    }
    with patch("diagnose_checkout_abandonment.requests.post", return_value=_fake_response(body)):
        result = _call(checkout_stage="otp_entry", minutes_since_abandonment=10)
    assert result["reason"] == "distraction_or_multitasking"


def test_model_can_return_a_reason_outside_the_known_enum():
    # Local models don't strictly enforce a JSON-schema enum on tool-call
    # arguments - diagnose_abandonment_reason() deliberately does not
    # validate against AbandonmentReason itself, mirroring
    # diagnose_decline_code()/detect_at_risk(). That validation is
    # checkout_abandonment_agent.py's job.
    body = {
        "message": {
            "tool_calls": [
                {"function": {"arguments": {"reason": "aliens_intervened", "reasoning": "hallucinated"}}}
            ]
        }
    }
    with patch("diagnose_checkout_abandonment.requests.post", return_value=_fake_response(body)):
        result = _call()
    assert result["reason"] == "aliens_intervened"


def test_transient_failure_then_success_retries_and_recovers():
    body = {
        "message": {
            "tool_calls": [{"function": {"arguments": {"reason": "otp_delay_or_failure", "reasoning": "ok"}}}]
        }
    }
    with patch(
        "diagnose_checkout_abandonment.requests.post",
        side_effect=[requests.exceptions.ConnectionError("transient"), _fake_response(body)],
    ) as mock_post, patch("diagnose_checkout_abandonment.time.sleep"):
        result = _call(checkout_stage="otp_entry", minutes_since_abandonment=2)
    assert result["reason"] == "otp_delay_or_failure"
    assert mock_post.call_count == 2


def test_all_retries_exhausted_falls_back_without_raising():
    with patch(
        "diagnose_checkout_abandonment.requests.post",
        side_effect=requests.exceptions.ConnectionError("ollama is down"),
    ) as mock_post, patch("diagnose_checkout_abandonment.time.sleep"):
        result = _call()
    assert result["reason"] is None
    assert mock_post.call_count == diag.MAX_RETRIES


def test_prompt_never_leaks_the_ground_truth_field_name():
    # Structural guard: the prompt this function builds is only ever
    # constructed from the four signal params + amount - there is no
    # abandonment_reason parameter for a caller to accidentally pass in,
    # so the ground truth simply cannot leak through this function's
    # signature. Verified by inspecting the real function signature
    # rather than trusting the docstring's claim.
    import inspect
    params = list(inspect.signature(diag.diagnose_abandonment_reason).parameters)
    assert "abandonment_reason" not in params
    assert set(params) == {
        "checkout_stage", "minutes_since_abandonment", "device_type",
        "is_returning_customer", "amount_paise",
    }


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
