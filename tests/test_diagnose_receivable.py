"""
Unit tests for diagnose_receivable.py - mirrors tests/test_diagnose.py's
and tests/test_diagnose_checkout_abandonment.py's mocking style exactly:
mock requests.post directly so these run in milliseconds with no real
Ollama server needed.

diagnose_receivable() is designed to never raise, same contract as every
prior diagnosis stage in this project: any Ollama hiccup or malformed
tool call must fall back to {"case_reason": None, ...} so the caller
(receivables_agent.py) can flag the record for manual review instead of
crashing a batch run.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import requests

import diagnose_receivable as diag


def _fake_response(json_body: dict):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_body
    return resp


def _call(**overrides):
    kwargs = dict(
        days_overdue=5,
        payment_terms="net_30",
        customer_payment_history_signal="first_time_overdue",
        reminders_sent_count=0,
        last_reminder_response=None,
        amount_vs_typical_ratio=1.0,
    )
    kwargs.update(overrides)
    return diag.diagnose_receivable(**kwargs)


def test_no_tool_call_returns_none_case_reason():
    with patch("diagnose_receivable.requests.post", return_value=_fake_response({"message": {}})):
        result = _call()
    assert result["case_reason"] is None
    assert "no tool call" in result["reasoning"].lower()


def test_malformed_tool_call_arguments_returns_none_case_reason():
    body = {"message": {"tool_calls": [{"function": {"arguments": "{not valid json"}}]}}
    with patch("diagnose_receivable.requests.post", return_value=_fake_response(body)):
        result = _call()
    assert result["case_reason"] is None
    assert "malformed" in result["reasoning"].lower()


def test_missing_case_reason_field_returns_none():
    body = {"message": {"tool_calls": [{"function": {"arguments": {"reasoning": "no case_reason key at all"}}}]}}
    with patch("diagnose_receivable.requests.post", return_value=_fake_response(body)):
        result = _call()
    assert result["case_reason"] is None
    assert "case_reason" in result["reasoning"].lower()


def test_correct_classification_of_an_unambiguous_case():
    body = {
        "message": {
            "tool_calls": [
                {"function": {"arguments": {
                    "case_reason": "chronic_late_payer_will_eventually_pay",
                    "reasoning": "History says this business always pays late but pays eventually.",
                }}}
            ]
        }
    }
    with patch("diagnose_receivable.requests.post", return_value=_fake_response(body)):
        result = _call(
            customer_payment_history_signal="always_pays_late_but_pays",
            days_overdue=20,
            reminders_sent_count=1,
            last_reminder_response="no_response",
        )
    assert result["case_reason"] == "chronic_late_payer_will_eventually_pay"
    assert "history" in result["reasoning"].lower() or "late" in result["reasoning"].lower()


def test_ambiguous_case_can_be_misdiagnosed_and_is_passed_through_unvalidated():
    # first_time_overdue + 0 reminders + days_overdue<=10 is the
    # deliberately-ambiguous cluster shared by cash_flow_delay and
    # payment_process_friction (generate_receivables_data.py). If the
    # model guesses the wrong one, diagnose_receivable() must not
    # "correct" it - it passes through exactly what the model said.
    body = {
        "message": {
            "tool_calls": [
                {"function": {"arguments": {
                    "case_reason": "payment_process_friction",
                    "reasoning": "A first-time overdue invoice this early with no reminders yet could just as easily be a cash-flow timing issue.",
                }}}
            ]
        }
    }
    with patch("diagnose_receivable.requests.post", return_value=_fake_response(body)):
        result = _call(
            days_overdue=5, customer_payment_history_signal="first_time_overdue",
            reminders_sent_count=0, last_reminder_response=None, amount_vs_typical_ratio=1.1,
        )
    assert result["case_reason"] == "payment_process_friction"


def test_model_can_return_a_case_reason_outside_the_known_enum():
    # Local models don't strictly enforce a JSON-schema enum on tool-call
    # arguments - diagnose_receivable() deliberately does not validate
    # against ReceivableReason itself, mirroring every prior diagnosis
    # stage. That validation is receivables_agent.py's job.
    body = {
        "message": {
            "tool_calls": [
                {"function": {"arguments": {"case_reason": "aliens_intervened", "reasoning": "hallucinated"}}}
            ]
        }
    }
    with patch("diagnose_receivable.requests.post", return_value=_fake_response(body)):
        result = _call()
    assert result["case_reason"] == "aliens_intervened"


def test_transient_failure_then_success_retries_and_recovers():
    body = {
        "message": {
            "tool_calls": [{"function": {"arguments": {"case_reason": "cash_flow_delay", "reasoning": "ok"}}}]
        }
    }
    with patch(
        "diagnose_receivable.requests.post",
        side_effect=[requests.exceptions.ConnectionError("transient"), _fake_response(body)],
    ) as mock_post, patch("diagnose_receivable.time.sleep"):
        result = _call()
    assert result["case_reason"] == "cash_flow_delay"
    assert mock_post.call_count == 2


def test_all_retries_exhausted_falls_back_without_raising():
    with patch(
        "diagnose_receivable.requests.post",
        side_effect=requests.exceptions.ConnectionError("ollama is down"),
    ) as mock_post, patch("diagnose_receivable.time.sleep"):
        result = _call()
    assert result["case_reason"] is None
    assert mock_post.call_count == diag.MAX_RETRIES


def test_prompt_never_leaks_the_ground_truth_field_name():
    # Structural guard: the prompt this function builds is only ever
    # constructed from the five signal params - there is no case_reason
    # parameter for a caller to accidentally pass in, so the ground truth
    # simply cannot leak through this function's signature. Verified by
    # inspecting the real function signature rather than trusting the
    # docstring's claim.
    import inspect
    params = list(inspect.signature(diag.diagnose_receivable).parameters)
    assert "case_reason" not in params
    assert set(params) == {
        "days_overdue", "payment_terms", "customer_payment_history_signal",
        "reminders_sent_count", "last_reminder_response", "amount_vs_typical_ratio",
    }


def test_no_reminder_sent_yet_is_handled_without_a_response_value():
    body = {
        "message": {
            "tool_calls": [{"function": {"arguments": {"case_reason": "cash_flow_delay", "reasoning": "ok"}}}]
        }
    }
    with patch("diagnose_receivable.requests.post", return_value=_fake_response(body)) as mock_post:
        result = _call(reminders_sent_count=0, last_reminder_response=None)
    assert result["case_reason"] == "cash_flow_delay"
    prompt = mock_post.call_args.kwargs["json"]["messages"][0]["content"]
    assert "no reminder sent yet" in prompt.lower()


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
