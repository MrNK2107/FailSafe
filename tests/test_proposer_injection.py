"""The recovery pipeline's "propose" step is injected, not mock-sniffed."""

import asyncio
from unittest.mock import patch

from failsafe import recovery_pipeline  # noqa: E402

ARGS = ({"subscription_id": "s1"}, "desc", "bank")
KW = {"situation": "sit", "id_field": "subscription_id", "record_label": "Subscription"}


def test_sync_injected_proposer_is_called():
    calls = []

    def fake(record, description, source, **kw):
        calls.append((record, description, source, kw))
        return {"action": "delayed_retry", "reasoning": "fake"}

    with recovery_pipeline.use_proposer(fake):
        out = asyncio.run(recovery_pipeline._call_propose_action(*ARGS, **KW))
    assert out["action"] == "delayed_retry"
    assert calls == [(*ARGS, KW)]
    assert recovery_pipeline.proposer is None


def test_async_injected_proposer_is_awaited():
    async def fake(*a, **k):
        return {"action": "flag_for_manual_review", "reasoning": "async"}

    previous = recovery_pipeline.set_proposer(fake)
    try:
        out = asyncio.run(recovery_pipeline._call_propose_action(*ARGS, **KW))
    finally:
        recovery_pipeline.set_proposer(previous)
    assert out["action"] == "flag_for_manual_review"


def test_default_uses_real_async_ollama_client():
    async def fake_async(*a, **k):
        return {"action": None, "reasoning": "llm down"}

    with patch("failsafe.ollama_client.propose_action_async", fake_async):
        out = asyncio.run(recovery_pipeline._call_propose_action(*ARGS, **KW))
    assert out == {"action": None, "reasoning": "llm down"}
