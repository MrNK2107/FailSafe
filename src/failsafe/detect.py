import time

import requests

from failsafe.llm_client import (
    MAX_RETRIES,
    OLLAMA_HOST,
    OLLAMA_MODEL,
    RETRY_BACKOFF_SECONDS,
    OllamaToolCaller,
)

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

_caller = OllamaToolCaller(
    host=OLLAMA_HOST, model=OLLAMA_MODEL, timeout=120, retries=MAX_RETRIES, keep_alive="30m"
)


def detect_at_risk(
    previous_retry_count: int,
    days_since_last_successful_charge: int,
    most_recent_gateway_response: str | None,
    subscription_status: str,
) -> dict:
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
        f'Most recent gateway/bank response: "{gateway_response_text}"\n'
        f"Subscription status field: {subscription_status}\n\n"
        "Call record_detection with the single best classification and a short reason."
    )
    messages = [{"role": "user", "content": prompt}]
    result = _caller.call_tool_sync(
        messages, [DETECT_TOOL], fallback_key="classification", fallback_value=None
    )
    if "classification" not in result:
        return {
            "classification": None,
            "reasoning": result.get("reasoning", "Model returned no tool call."),
        }
    return {
        "classification": result.get("classification"),
        "reasoning": result.get("reasoning", ""),
    }
