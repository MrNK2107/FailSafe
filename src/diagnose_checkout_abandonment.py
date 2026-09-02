"""
Root-cause diagnosis stage for CHECKOUT ABANDONMENT - "why did this
customer abandon?", inferred, not handed as ground truth. Mirrors
diagnose.py's non-negotiable rule exactly: a stage that infers something
but is then ignored by the rest of the pipeline is theater, not a fix
(PS_REQUIREMENTS_DEBATE.md Round 2's own framing) - see
checkout_abandonment_agent.py for where the diagnosed reason actually
drives the policy lookup and the executed action.

A deliberate design difference from diagnose.py, stated honestly rather
than silently copied: diagnose.py infers a decline_code from a raw,
free-text bank/gateway MESSAGE, because that's genuinely how a real
payment decline surfaces (a decline reason string). A checkout funnel has
no equivalent free-text artifact for an abandoned session - there is no
human/bank writing a sentence about why someone closed a tab. What a real
checkout funnel DOES emit is structured telemetry: which stage the
customer reached, how long ago they left, what device they were on, and
whether they've bought before. So this stage takes the same shape as
detect.py (structured signals in, one classification out) rather than
diagnose.py's free-text shape - the more defensible modeling choice for
this domain, not an oversight or a missed opportunity to reuse code.

Given ONLY: checkout_stage, minutes_since_abandonment, device_type,
is_returning_customer, and (as context, like diagnose.py's amount_paise)
amount_paise. NEVER given the ground-truth abandonment_reason -
generate_checkout_abandonment_data.py keeps that field only for scoring,
exactly like decline_code is kept in generate_data.py only for scoring
diagnose.py's accuracy.

Same conventions as diagnose.py/detect.py: reuses ollama_client's
constants directly (one local model, one config surface, one retry
budget), a dedicated tool schema (record_abandonment_diagnosis - a
FOURTH distinct tool schema in this project, after record_decision,
record_diagnosis, and record_detection - each pipeline stage gets its own
schema on purpose), temperature 0 (classification, not creative
generation), and designed to NEVER raise - any Ollama hiccup or malformed
tool call returns {"reason": None, "reasoning": "<failure note>"} so a
single bad diagnosis can never crash a batch.

Deliberately does NOT validate the returned reason against
AbandonmentReason here, mirroring diagnose_decline_code's/detect_at_risk's
own choice to leave that check to the caller
(checkout_abandonment_agent.py) - kept symmetric with the rest of the
project on purpose, not an oversight.
"""

import json
import time

import requests

from ollama_client import MAX_RETRIES, OLLAMA_HOST, OLLAMA_MODEL, RETRY_BACKOFF_SECONDS

_REASON_DESCRIPTIONS = {
    "otp_delay_or_failure": "OTP/3-D Secure authentication timed out, failed, or never arrived - a technical/delivery problem, not a customer decision",
    "payment_method_unsupported": "the customer's preferred or only payment method wasn't accepted or failed validation",
    "price_shock": "the customer reached final review, saw the total amount, and balked - a price-sensitivity signal",
    "distraction_or_multitasking": "nothing about the checkout itself seems to be the cause - the customer was probably just pulled away",
    "trust_or_security_concern": "signals suggest hesitation specifically about sharing payment details, often a new customer at the card-entry step",
}
_SORTED_REASONS = sorted(_REASON_DESCRIPTIONS.items())

DIAGNOSE_ABANDONMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "record_abandonment_diagnosis",
        "description": (
            "Record your diagnosis of why this customer abandoned checkout before "
            "completing any payment attempt. This is diagnosis ONLY - a separate, "
            "deterministic policy table (not this call) decides what recovery "
            "action to take based on your diagnosis."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": [reason for reason, _ in _SORTED_REASONS],
                    "description": (
                        "The single reason that best explains this abandonment. "
                        "Known reasons and what each means:\n"
                        + "\n".join(f"- {reason}: {desc}" for reason, desc in _SORTED_REASONS)
                        + "\n\nApply these rules IN ORDER:\n"
                        "1. checkout_stage is 'review_confirm' (the customer reached the FINAL "
                        "step, where the total price is shown) -> 'price_shock', UNLESS minutes_"
                        "since_abandonment is very long (roughly 30+ minutes), in which case treat "
                        "the long gap as the stronger signal and use rule 5 instead. Reaching this "
                        "stage at all means they already saw the total - do not call this "
                        "'distraction' just because there is no OTHER explicit price signal; "
                        "reaching review_confirm IS the price signal.\n"
                        "2. checkout_stage is 'otp_entry' AND minutes_since_abandonment is roughly "
                        "under 20 -> 'otp_delay_or_failure' (a short-to-medium gap right after "
                        "starting OTP entry is far more often a delivery delay or failed code than "
                        "a random interruption that just happened to strike at that exact step).\n"
                        "3. checkout_stage is 'payment_method_selection', OR checkout_stage is "
                        "'card_details_entry' AND is_returning_customer is true -> "
                        "'payment_method_unsupported' (a returning customer stalling at card entry "
                        "has done this before; the more likely new problem is their method/card "
                        "not working this time, not sudden distraction or new hesitation).\n"
                        "4. checkout_stage is 'card_details_entry' AND is_returning_customer is "
                        "false -> genuinely ambiguous between 'trust_or_security_concern' (a "
                        "nervous new customer) and 'payment_method_unsupported' (a new customer "
                        "whose card type isn't accepted) - weigh device_type and minutes as weak "
                        "tie-breakers, but expect to sometimes be wrong here; that is fine.\n"
                        "5. minutes_since_abandonment is long (roughly 30+ minutes) AND none of "
                        "the rules above already matched more specifically -> "
                        "'distraction_or_multitasking'. This is the ONLY reason that should be "
                        "picked mainly because of a LONG time gap. Do NOT use it as a generic "
                        "default just because nothing else seems to fit, and do NOT use it for a "
                        "short gap (under ~15-20 minutes) - a customer who left minutes ago hasn't "
                        "had time to get meaningfully 'distracted' yet, something about the "
                        "checkout step itself is the more likely explanation.\n\n"
                        "Worked examples, including corrections of a common mistake: "
                        "review_confirm, 4 minutes -> price_shock, NOT distraction (they reached "
                        "the final total and left almost immediately - that is price sensitivity, "
                        "not multitasking). otp_entry, 3 minutes -> otp_delay_or_failure, NOT "
                        "distraction (too soon after starting OTP entry to be an unrelated "
                        "interruption). otp_entry, 45 minutes -> distraction_or_multitasking "
                        "(now the long gap IS the dominant signal). card_details_entry, returning "
                        "customer, 5 minutes -> payment_method_unsupported, NOT distraction (a "
                        "repeat customer suddenly stalling at card entry points at the method, not "
                        "a new attention lapse). Pick the single most likely reason given only the "
                        "signals below and nothing else. You will not always be right on a "
                        "genuinely ambiguous case (rule 4), and that is expected."
                    ),
                },
                "reasoning": {
                    "type": "string",
                    "description": "One or two sentences on why this reason best fits these signals.",
                },
            },
            "required": ["reason", "reasoning"],
        },
    },
}


def diagnose_abandonment_reason(
    checkout_stage: str,
    minutes_since_abandonment: int,
    device_type: str,
    is_returning_customer: bool,
    amount_paise: int | None = None,
) -> dict:
    """
    Ask the local model to infer WHY a checkout was abandoned from ONLY
    structured funnel signals - never the ground-truth abandonment_reason.
    Returns {"reason": str, "reasoning": str} on success, or
    {"reason": None, "reasoning": "<failure note>"} on ANY failure -
    malformed tool call, HTTP error, timeout, whatever. Mirrors
    diagnose_decline_code's/detect_at_risk's never-raise contract exactly:
    the caller treats a None reason as "flag for manual review", which
    must never crash a batch run.
    """
    context = f"\nCart amount: Rs {amount_paise / 100:.2f}" if amount_paise is not None else ""
    prompt = (
        "A customer started checkout but never completed a payment attempt - "
        "no payment was ever declined, because none was ever attempted. Below "
        "are the only signals available about the abandoned session. You have "
        "NOT been told why they actually left.\n\n"
        f"Checkout stage reached: {checkout_stage}\n"
        f"Minutes since abandonment: {minutes_since_abandonment}\n"
        f"Device type: {device_type}\n"
        f"Returning customer: {is_returning_customer}"
        f"{context}\n\n"
        "Call record_abandonment_diagnosis with the single reason that best "
        "explains this abandonment and a short reason."
    )

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                f"{OLLAMA_HOST}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "tools": [DIAGNOSE_ABANDONMENT_TOOL],
                    "stream": False,
                    "keep_alive": "30m",
                    "options": {"temperature": 0},
                },
                timeout=120,
            )
            resp.raise_for_status()
            message = resp.json().get("message", {})
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                return {"reason": None, "reasoning": "Model returned no tool call."}

            args = tool_calls[0].get("function", {}).get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    return {"reason": None, "reasoning": "Model returned malformed tool-call arguments."}

            reason = args.get("reason")
            reasoning = args.get("reasoning", "")
            if not reason:
                return {"reason": None, "reasoning": "Model tool call missing 'reason' field."}

            return {"reason": reason, "reasoning": reasoning}

        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    return {"reason": None, "reasoning": f"Ollama request failed after {MAX_RETRIES} attempts: {last_error}"}
