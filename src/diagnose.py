"""
Root-cause diagnosis stage.

Before this file existed, `decline_code` was assigned as ground truth by
generate_data.py's weighted RNG and consumed everywhere downstream
(gate.py, ollama_client.py) as a given fact - there was no step anywhere
in the pipeline that actually inferred a decline_code from anything. The
track's own problem statement names this as its own pipeline stage,
distinct from intervention-selection ("diagnosing it" in the why-now
paragraph; "Payment degradation -> root cause -> recovery action" as
example direction 1) - see PS_REQUIREMENTS_DEBATE.md Round 2 for the full
finding this file closes.

This function is given ONLY `raw_decline_message` (a raw, human/bank-style
decline string - see generate_data.py's RAW_SIGNAL_TEMPLATES) and infers a
decline_code from it. It is never given the ground-truth decline_code or
its config/decline_policy.json description - if it were, this would be a
lookup wearing a diagnosis costume, not a real classification task. The
caller (recovery_pipeline.py) is responsible for comparing the diagnosed
code against ground truth ONLY for audit-log measurement, never for
feeding it back into this function.

Same pattern as ollama_client.propose_action(), reusing its constants
directly (same Ollama host/model/retry budget - one local model, one
config surface): a DIFFERENT tool schema (record_diagnosis, not
record_decision - diagnosis and intervention-selection are explicitly two
different stages per the problem statement), temperature 0 (this is
classification, not creative generation - same reasoning as
propose_action), and designed to NEVER raise - any Ollama hiccup or
malformed tool call returns {"decline_code": None, "reasoning": "<failure
note>"} so a single bad diagnosis can never crash a batch run.

Deliberately does NOT validate the returned decline_code against
DECLINE_CODES here - propose_action doesn't validate its `action` against
the RecoveryAction enum either, leaving that check to the caller
(recovery_pipeline.py, mirroring its existing llm_invalid_action handling
for actions). Keeping the two symmetric was a deliberate choice, not an
oversight.
"""

import json
import time

import requests

from decline_codes import DECLINE_CODES
from ollama_client import MAX_RETRIES, OLLAMA_HOST, OLLAMA_MODEL, RETRY_BACKOFF_SECONDS

# Sorted so the enum (and the prompt-embedded description list) is
# deterministic across runs - matters for reproducing a diagnosis run
# exactly, and for tests that assert on prompt content.
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
                        + "\n".join(f"- {code}: {policy.description}" for code, policy in _SORTED_CODES)
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


def diagnose_decline_code(raw_decline_message: str, amount_paise: int | None = None) -> dict:
    """
    Ask the local model to infer a decline_code from ONLY a raw decline
    message. Returns {"decline_code": str, "reasoning": str} on success, or
    {"decline_code": None, "reasoning": "<failure note>"} on ANY failure -
    malformed tool call, HTTP error, timeout, whatever. Mirrors
    ollama_client.propose_action's never-raise contract exactly, for the
    same reason: the caller (recovery_pipeline.py) treats a None diagnosis
    as "flag for manual review", which must never crash a batch run.

    Note what is deliberately NOT passed in: no decline_code, no
    config/decline_policy.json description, no decline source. Only the
    raw message and (optionally) the amount, which is realistic context a
    human reviewer would also have and does not leak the answer.
    """
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

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                f"{OLLAMA_HOST}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "tools": [DIAGNOSE_TOOL],
                    "stream": False,
                    # Same reasoning as ollama_client.py: keep the model
                    # resident between calls instead of reloading it from
                    # disk on every single request.
                    "keep_alive": "30m",
                    # Classification task, not creative generation -
                    # deterministic output is correct here, same as
                    # propose_action.
                    "options": {"temperature": 0},
                },
                timeout=120,
            )
            resp.raise_for_status()
            message = resp.json().get("message", {})
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                return {"decline_code": None, "reasoning": "Model returned no tool call."}

            args = tool_calls[0].get("function", {}).get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    return {"decline_code": None, "reasoning": "Model returned malformed tool-call arguments."}

            decline_code = args.get("decline_code")
            reasoning = args.get("reasoning", "")
            if not decline_code:
                return {"decline_code": None, "reasoning": "Model tool call missing 'decline_code' field."}

            return {"decline_code": decline_code, "reasoning": reasoning}

        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    return {"decline_code": None, "reasoning": f"Ollama request failed after {MAX_RETRIES} attempts: {last_error}"}
