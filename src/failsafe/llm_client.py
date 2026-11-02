import asyncio
import json
import logging
import os
import time

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

import requests
from dotenv import load_dotenv

load_dotenv()

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 3

logger = logging.getLogger(__name__)


class OllamaToolCaller:
    def __init__(self, host=None, model=None, timeout=120, retries=3, keep_alive="30m"):
        self.host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3.1:8b")
        self.timeout = timeout
        self.retries = retries
        self.keep_alive = keep_alive

    def _parse_tool_call(self, data: dict, fallback_key: str = "action"):
        message = data.get("message", {})
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return None, "Model returned no tool call."
        args = tool_calls[0].get("function", {}).get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return None, "Model returned malformed tool-call arguments."
        if not args.get(fallback_key) and fallback_key != "_any":
            has_any = any(
                k in args
                for k in ("action", "classification", "decline_code", "reason", "case_reason")
            )
            if not has_any:
                return None, f"Model tool call missing '{fallback_key}' field."
        return args, ""

    def call_tool_sync(self, messages, tools, fallback_key="action", fallback_value=None):
        last_error = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = requests.post(
                    f"{self.host}/api/chat",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "tools": tools,
                        "stream": False,
                        "keep_alive": self.keep_alive,
                        "options": {"temperature": 0},
                    },
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                args, err = self._parse_tool_call(data, fallback_key=fallback_key)
                if args is None:
                    return (
                        {fallback_key: fallback_value, "reasoning": err}
                        if fallback_key != "_any"
                        else {"reasoning": err}
                    )
                return args
            except requests.exceptions.RequestException as e:
                last_error = e
                logger.warning("Ollama request failed attempt %s/%s: %s", attempt, self.retries, e)
                if attempt < self.retries:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        return {
            fallback_key: fallback_value,
            "reasoning": f"Ollama request failed after {self.retries} attempts: {last_error}",
        }

    async def call_tool(self, messages, tools, fallback_key="action", fallback_value=None):
        if httpx is not None:
            last_error = None
            for attempt in range(1, self.retries + 1):
                try:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        resp = await client.post(
                            f"{self.host}/api/chat",
                            json={
                                "model": self.model,
                                "messages": messages,
                                "tools": tools,
                                "stream": False,
                                "keep_alive": self.keep_alive,
                                "options": {"temperature": 0},
                            },
                        )
                        resp.raise_for_status()
                        data = resp.json()
                        args, err = self._parse_tool_call(data, fallback_key=fallback_key)
                        if args is None:
                            return (
                                {fallback_key: fallback_value, "reasoning": err}
                                if fallback_key != "_any"
                                else {"reasoning": err}
                            )
                        return args
                except Exception as e:
                    last_error = e
                    logger.warning(
                        "Ollama async request failed attempt %s/%s: %s", attempt, self.retries, e
                    )
                    if attempt < self.retries:
                        await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
            return {
                fallback_key: fallback_value,
                "reasoning": f"Ollama request failed after {self.retries} attempts: {last_error}",
            }
        else:
            return await asyncio.to_thread(
                self.call_tool_sync, messages, tools, fallback_key, fallback_value
            )

    def healthcheck(self) -> bool:
        try:
            resp = requests.get(f"{self.host}/api/tags", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    async def healthcheck_async(self) -> bool:
        if httpx is not None:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(f"{self.host}/api/tags")
                    return resp.status_code == 200
            except Exception:
                return False
        return await asyncio.to_thread(self.healthcheck)


def healthcheck() -> tuple[bool, str]:
    """Module-level Ollama reachability check returning (ok, human-readable message)."""
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        if resp.status_code == 200:
            return True, f"Ollama reachable at {OLLAMA_HOST} ({OLLAMA_MODEL})"
        return False, f"Ollama at {OLLAMA_HOST} returned HTTP {resp.status_code}"
    except Exception as e:
        return False, (
            f"Ollama not reachable at {OLLAMA_HOST}: {e}. Is Ollama running? "
            f"Try: ollama serve  (and ollama pull {OLLAMA_MODEL})"
        )


_default_caller = OllamaToolCaller(
    host=OLLAMA_HOST, model=OLLAMA_MODEL, timeout=120, retries=MAX_RETRIES, keep_alive="30m"
)

DECIDE_ACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "record_decision",
        "description": "Record your decision for what to do about this halted subscription.",
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
                    "description": "Choose exactly one action.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "One or two sentences on why this action fits.",
                },
            },
            "required": ["action", "reasoning"],
        },
    },
}

DEFAULT_SITUATION = (
    "A subscription payment was halted after Razorpay's automatic 3-day retry cycle failed 3 times."
)


def propose_action(
    record: dict,
    decline_description: str,
    decline_source: str,
    situation: str = DEFAULT_SITUATION,
    id_field: str = "subscription_id",
    record_label: str = "Subscription",
) -> dict:
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
    return _default_caller.call_tool_sync(
        messages, [DECIDE_ACTION_TOOL], fallback_key="action", fallback_value=None
    )
