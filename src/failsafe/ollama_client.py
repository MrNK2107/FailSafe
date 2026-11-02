import argparse
import time

import requests

from failsafe.llm_client import DEFAULT_SITUATION as _DEFAULT_SITUATION
from failsafe.llm_client import MAX_RETRIES as _MAX_RETRIES
from failsafe.llm_client import OLLAMA_HOST as _OLLAMA_HOST
from failsafe.llm_client import OLLAMA_MODEL as _OLLAMA_MODEL
from failsafe.llm_client import OllamaToolCaller
from failsafe.llm_client import propose_action as _llm_propose_action

OLLAMA_HOST = _OLLAMA_HOST
OLLAMA_MODEL = _OLLAMA_MODEL
MAX_RETRIES = _MAX_RETRIES
RETRY_BACKOFF_SECONDS = 3
DEFAULT_SITUATION = _DEFAULT_SITUATION

DECIDE_ACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "record_decision",
        "description": (
            "Record your decision for what to do about this halted subscription. "
            "This does NOT execute anything by itself - a separate policy gate "
            "reviews and can override this decision before any real action happens."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "immediate_retry",
                        "delayed_retry",
                        "payment_link_nudge",
                        "no_action_fraud",
                        "no_action_unrecoverable",
                    ],
                    "description": (
                        "Choose exactly one action. Apply these rules in order:\n"
                        "1. Bank flagged this decline specifically as fraud/risk -> 'no_action_fraud'.\n"
                        "2. The CUSTOMER must personally do something before this can ever succeed "
                        "(supply a new/updated card, enter a correct CVV, re-enter an OTP/re-authenticate, "
                        "activate the card for online use, or simply attempt payment again some other "
                        "way since the bank gave no further detail) -> 'payment_link_nudge'. This "
                        "includes an expired card, a card disabled or inactive for online use, a wrong "
                        "CVV, a failed OTP/authentication (including 'browser closure during "
                        "verification' - that IS a failed authentication, NOT a funds issue), AND any "
                        "decline description that just says 'declined by the bank' or 'payment failed' "
                        "with NO mention of downtime, a timeout, or a technical/system error - a bare "
                        "bank decline with no further detail is not something a blind retry fixes. "
                        "These are all customer-FIXABLE, not unrecoverable and not a reason to wait.\n"
                        "3. decline_source is 'network', 'gateway', or 'bank' AND the description "
                        "ITSELF explicitly names a technical/system problem - the words timeout, "
                        "downtime, or technical error, not just 'declined' or 'failed' -> "
                        "'immediate_retry'. Do NOT apply this rule just because decline_source is "
                        "'bank' - a bank-sourced decline with no infra language in its description is "
                        "rule 2, not this rule.\n"
                        "4. decline_source is 'customer' AND the description is specifically about "
                        "available funds or a transaction/spending limit (not authentication, not the "
                        "card itself) -> 'delayed_retry' (give it time to clear, e.g. next payday).\n"
                        "5. ONLY if the customer explicitly cancelled/walked away, or the bank has "
                        "permanently blocked/closed the instrument with no customer fix available -> "
                        "'no_action_unrecoverable'. Do NOT use this for a merely expired, disabled, "
                        "inactive, or wrong-CVV card - those are rule 2.\n\n"
                        "Worked examples, including corrections of common mistakes: card_expired -> "
                        "payment_link_nudge (customer supplies a new card, not unrecoverable). "
                        "payment_timed_out, source=network, description mentions exceeding a time "
                        "limit -> immediate_retry (explicit infra language). card_declined, "
                        "source=bank, description just says 'declined by the bank' with NO infra "
                        "language -> payment_link_nudge, NOT immediate_retry (bank source alone is "
                        "not enough for rule 3). authentication_failed, 'incorrect OTP entry' -> "
                        "payment_link_nudge, NOT delayed_retry (a failed authentication is "
                        "customer-actionable right now, not a funds/limit issue to wait out). "
                        "insufficient_funds, source=customer, about funds -> delayed_retry. customer "
                        "cancelled the transaction -> no_action_unrecoverable."
                    ),
                },
                "reasoning": {
                    "type": "string",
                    "description": "One or two sentences on why this action fits this case.",
                },
            },
            "required": ["action", "reasoning"],
        },
    },
}


def healthcheck() -> tuple[bool, str]:
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        if resp.status_code == 200:
            return True, f"Ollama reachable at {OLLAMA_HOST} ({OLLAMA_MODEL})"
        return False, f"Ollama at {OLLAMA_HOST} returned HTTP {resp.status_code}"
    except requests.exceptions.ConnectTimeout:
        reason = "no answer within 5 s"
    except requests.exceptions.ConnectionError:
        # urllib3's own text is a 300-character pool/retry dump; say what it means
        reason = "connection refused or host down"
    except requests.exceptions.Timeout:
        reason = "no answer within 5 s"
    except Exception as e:
        reason = str(e) or type(e).__name__
    return False, (
        f"Ollama not reachable at {OLLAMA_HOST}: {reason}. Is Ollama running? "
        f"Try: ollama serve  (and ollama pull {OLLAMA_MODEL}). Set OLLAMA_HOST if it runs elsewhere."
    )


def require_ollama(skip: bool = False) -> None:
    """Fail fast, before any record is touched, when Ollama is unreachable.

    Without this the domain agents retried every record against a dead host
    (3 attempts x backoff each) and the one-time agent quietly wrote a
    RESULTS file in which every record had fallen back to policy.
    """
    if skip:
        return
    ok, msg = healthcheck()
    if not ok:
        raise SystemExit(f"Healthcheck failed: {msg} (use --skip-healthcheck to bypass)")
    print(f"[healthcheck] {msg}")


def non_negative_int(value: str) -> int:
    """argparse type for a record count: a clear error instead of a traceback,
    and no silent slicing from the end for negative numbers."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a whole number of records, got {value!r}"
        ) from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"record count must be 0 or more, got {n}")
    return n


def propose_action(
    record: dict,
    decline_description: str,
    decline_source: str,
    situation: str = DEFAULT_SITUATION,
    id_field: str = "subscription_id",
    record_label: str = "Subscription",
) -> dict:
    return _llm_propose_action(
        record,
        decline_description,
        decline_source,
        situation=situation,
        id_field=id_field,
        record_label=record_label,
    )


async def propose_action_async(
    record: dict,
    decline_description: str,
    decline_source: str,
    situation: str = DEFAULT_SITUATION,
    id_field: str = "subscription_id",
    record_label: str = "Subscription",
) -> dict:
    caller = OllamaToolCaller(
        host=OLLAMA_HOST, model=OLLAMA_MODEL, timeout=120, retries=MAX_RETRIES, keep_alive="30m"
    )
    prompt = (
        f"{situation}\n\n"
        f"{record_label}: {record[id_field]}\n"
        f"Amount: Rs {record['amount_paise'] / 100:.2f}\n"
        f"Decline code: {record['decline_code']}\n"
        f"Decline description: {decline_description}\n"
        f"Decline source: {decline_source}\n\n"
        f"Call record_decision with the single best next action and a short reason."
    )
    messages = [{"role": "user", "content": prompt}]
    return await caller.call_tool(
        messages, [DECIDE_ACTION_TOOL], fallback_key="action", fallback_value=None
    )
