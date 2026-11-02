import time

import requests

from failsafe.decline_codes import DECLINE_CODES
from failsafe.llm_client import MAX_RETRIES, OLLAMA_HOST, OLLAMA_MODEL, OllamaToolCaller

_SORTED_CODES = sorted(DECLINE_CODES.items())

DIAGNOSE_TOOL = {
    "type": "function",
    "function": {
        "name": "record_diagnosis",
        "description": (
            "Record your diagnosis of which decline code best explains this raw "
            "bank/gateway decline message. This is diagnosis ONLY - it does not "
            "decide what to do about it. A separate step (not this one) decides "
            "the recovery action, and a deterministic policy gate reviews that "
            "decision afterwards."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "decline_code": {
                    "type": "string",
                    "enum": [code for code, _ in _SORTED_CODES],
                    "description": (
                        "The single decline code that best matches the raw message. "
                        "Known codes and what each means:\n"
                        + "\n".join(
                            f"- {code}: {policy.description}" for code, policy in _SORTED_CODES
                        )
                        + "\n\nSome raw messages are genuinely ambiguous between two or "
                        "more codes - for example a generic 'do not honor' bank response "
                        "can mean an ordinary decline OR an undisclosed risk/fraud hold, "
                        "since banks deliberately avoid revealing fraud-detection logic "
                        "in the decline text itself. Pick the single most likely code "
                        "given only the raw message and nothing else. You will not "
                        "always be right on an ambiguous message, and that is expected "
                        "- do not guess a rare code just to seem thorough."
                    ),
                },
                "reasoning": {
                    "type": "string",
                    "description": "One or two sentences on why this code best fits the raw message.",
                },
            },
            "required": ["decline_code", "reasoning"],
        },
    },
}

_caller = OllamaToolCaller(
    host=OLLAMA_HOST, model=OLLAMA_MODEL, timeout=120, retries=MAX_RETRIES, keep_alive="30m"
)


def diagnose_decline_code(raw_decline_message: str, amount_paise: int | None = None) -> dict:
    context = f"\nAmount: Rs {amount_paise / 100:.2f}" if amount_paise is not None else ""
    prompt = (
        "A payment was declined. Below is the raw decline message exactly as "
        "returned by the bank/gateway. You have NOT been told any internal "
        "decline code or category - only this raw text.\n\n"
        f'"{raw_decline_message}"'
        f"{context}\n\n"
        "Call record_diagnosis with the single decline code that best explains "
        "this message and a short reason."
    )
    messages = [{"role": "user", "content": prompt}]
    result = _caller.call_tool_sync(
        messages, [DIAGNOSE_TOOL], fallback_key="decline_code", fallback_value=None
    )
    if "decline_code" not in result:
        return {
            "decline_code": None,
            "reasoning": result.get("reasoning", "Model returned no tool call."),
        }
    return {"decline_code": result.get("decline_code"), "reasoning": result.get("reasoning", "")}
