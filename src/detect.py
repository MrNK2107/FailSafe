"""
Revenue-at-risk DETECTION stage - the third and earliest pipeline stage,
distinct from diagnosis (diagnose.py) and intervention-selection
(ollama_client.py). See PS_REQUIREMENTS_DEBATE.md Round 2, finding 4, for
the gap this closes: `agent.py` never actually detects anything - it
unconditionally processes every record in `data/halted_subscriptions.json`,
a file whose name and contents guarantee every record is already at-risk.
There was no step anywhere in the pipeline that looked at a MIX of
subscriptions (some healthy, some genuinely at-risk) and decided which
ones need attention at all.

That debate also judged the missing-detection gap "more defensible" than
missing diagnosis, because reproducing Razorpay's real T+3 halt cycle live
requires several real wall-clock days - a genuine one-week-build
constraint, not a skill gap. This module does NOT try to reproduce that
live wait (that would reintroduce the same infeasibility). Instead it
detects risk from signals a real merchant/Razorpay account would plausibly
already have on hand without waiting days: how many retries have already
happened, how long it's been since the last successful charge, the most
recent gateway/bank response (if any), and the subscription's own status
field. None of that requires a multi-day wait - a merchant's dashboard
already has all four fields the moment they're generated.

Deliberately NOT given: any precomputed "is_at_risk"/"needs_attention"
boolean. generate_detection_pool.py computes a ground-truth label for
measurement purposes only (exactly how generate_data.py keeps a
ground-truth decline_code that diagnose.py is never shown) - handing that
boolean to this function would make "detection" a lookup wearing a
costume, not a real classification task.

Same conventions as diagnose.py/ollama_client.py: a THIRD, separate tool
schema (record_detection - detection, diagnosis, and intervention
selection are three distinct stages per the problem statement's own
wording: "detects revenue at risk... diagnosing it... determines the
right intervention"), temperature 0 (classification, not creative
generation), same retry/backoff budget, and designed to NEVER raise - any
Ollama hiccup or malformed tool call returns
{"classification": None, "reasoning": "<failure note>"} so a single bad
detection call can never crash a batch.
"""

import json
import time

import requests

from ollama_client import MAX_RETRIES, OLLAMA_HOST, OLLAMA_MODEL, RETRY_BACKOFF_SECONDS

NEEDS_ATTENTION = "needs_recovery_attention"
LEAVE_ALONE = "leave_alone"

DETECT_TOOL = {
    "type": "function",
    "function": {
        "name": "record_detection",
        "description": (
            "Record your judgment of whether this subscription is genuinely at "
            "risk of failed/lost revenue and needs recovery attention, or is "
            "healthy and should be left alone. This is DETECTION ONLY - it does "
            "not diagnose why a decline happened (a separate step does that, only "
            "for records this step flags) and it does not decide what recovery "
            "action to take (a further separate step, reviewed by a deterministic "
            "gate, does that)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "classification": {
                    "type": "string",
                    "enum": [NEEDS_ATTENTION, LEAVE_ALONE],
                    "description": (
                        f"'{NEEDS_ATTENTION}' - this subscription shows real signs of "
                        "payment trouble: repeated retries, a stale gap since the last "
                        "successful charge, a status other than a clean active/current "
                        "state, or a recent gateway/bank response describing a failure. "
                        f"'{LEAVE_ALONE}' - this subscription is paying fine: no retries "
                        "(or a single isolated one that already recovered), a recent "
                        "successful charge, and no concerning gateway response. Some "
                        "cases are genuinely ambiguous - e.g. one retry a few days ago "
                        "with no further detail could be a resolved blip OR the start of "
                        "real trouble. Weigh ALL of the signals together rather than any "
                        "single one in isolation, and pick the single best label. You "
                        "will not always be right on a genuinely ambiguous case, and "
                        "that is expected."
                    ),
                },
                "reasoning": {
                    "type": "string",
                    "description": "One or two sentences on why this classification fits these signals.",
                },
            },
            "required": ["classification", "reasoning"],
        },
    },
}


def detect_at_risk(
    previous_retry_count: int,
    days_since_last_successful_charge: int,
    most_recent_gateway_response: str | None,
    subscription_status: str,
) -> dict:
    """
    Ask the local model to classify a subscription as needing recovery
    attention or safe to leave alone, given ONLY synthetic-but-realistic
    signals a merchant/Razorpay account would already have without waiting
    on a real multi-day retry cycle. Returns
    {"classification": "needs_recovery_attention"|"leave_alone", "reasoning": str}
    on success, or {"classification": None, "reasoning": "<failure note>"}
    on ANY failure - malformed tool call, HTTP error, timeout, whatever.
    Mirrors diagnose_decline_code's/propose_action's never-raise contract
    exactly, for the same reason: the caller (recovery_pipeline.py) must
    never let a single bad detection call crash a batch run.

    Note what is deliberately NOT passed in: no ground-truth "is this
    actually at risk" label of any kind - only the four raw signals named
    above, which is realistic context a human reviewer would also have.
    """
    gateway_response_text = (
        most_recent_gateway_response
        if most_recent_gateway_response
        else "(no gateway/bank response on file - no failed attempt recorded)"
    )
    prompt = (
        "Here are the current signals on file for one subscription. You have NOT "
        "been told whether it is actually at risk - only these raw signals.\n\n"
        f"Previous retry attempts so far: {previous_retry_count}\n"
        f"Days since last successful charge: {days_since_last_successful_charge}\n"
        f"Most recent gateway/bank response: \"{gateway_response_text}\"\n"
        f"Subscription status field: {subscription_status}\n\n"
        "Call record_detection with the single best classification and a short reason."
    )

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                f"{OLLAMA_HOST}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "tools": [DETECT_TOOL],
                    "stream": False,
                    # Same reasoning as diagnose.py/ollama_client.py: keep
                    # the model resident between calls instead of reloading
                    # it from disk on every single request.
                    "keep_alive": "30m",
                    # Classification task, not creative generation -
                    # deterministic output is correct here, same as the
                    # other two stages.
                    "options": {"temperature": 0},
                },
                timeout=120,
            )
            resp.raise_for_status()
            message = resp.json().get("message", {})
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                return {"classification": None, "reasoning": "Model returned no tool call."}

            args = tool_calls[0].get("function", {}).get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    return {"classification": None, "reasoning": "Model returned malformed tool-call arguments."}

            classification = args.get("classification")
            reasoning = args.get("reasoning", "")
            if not classification:
                return {"classification": None, "reasoning": "Model tool call missing 'classification' field."}

            return {"classification": classification, "reasoning": reasoning}

        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    return {"classification": None, "reasoning": f"Ollama request failed after {MAX_RETRIES} attempts: {last_error}"}
