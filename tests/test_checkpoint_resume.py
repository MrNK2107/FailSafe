import json
from pathlib import Path

from failsafe.decline_codes import RecoveryAction
from failsafe.gate import Gate


def _load_checkpoint(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_seed_from_checkpoint_idempotency_and_run_total():
    checkpoint = Path(__file__).parent.parent / "logs" / "results_checkpoint.jsonl"
    if not checkpoint.exists():
        pytest_skip = __import__("pytest").skip
        pytest_skip("no checkpoint file present")
    results = _load_checkpoint(checkpoint)
    acted = {"immediate_retry", "delayed_retry", "payment_link_nudge"}
    already_spent = sum(
        r["amount_paise"] for r in results if r["final_action"] in acted and r["gate_executed"]
    )
    already_seen = {
        (r["subscription_id"], r["final_action"]) for r in results if r["gate_executed"]
    }
    gate = Gate()
    gate.seed_from_checkpoint(already_spent, already_seen)
    assert gate._run_total_paise == already_spent
    assert gate._seen == already_seen
    if results:
        sample = next(
            (r for r in results if r["gate_executed"] and r["final_action"] in acted), None
        )
        if sample:
            key = (sample["subscription_id"], sample["final_action"])
            assert key in gate._seen
            decision = gate.evaluate(
                sample["subscription_id"],
                sample["decline_code"],
                RecoveryAction(sample["final_action"]),
                sample["amount_paise"],
            )
            assert decision.execute is False
            assert "Duplicate" in decision.reason


def test_seed_from_checkpoint_run_total_blocks_when_cap_exceeded():
    gate = Gate()
    gate.seed_from_checkpoint(0, set())
    from failsafe.gate import MAX_RUN_TOTAL_PAISE

    almost_full = MAX_RUN_TOTAL_PAISE - 1000
    gate2 = Gate()
    gate2.seed_from_checkpoint(almost_full, set())
    decision = gate2.evaluate("sub_new", "insufficient_funds", RecoveryAction.DELAYED_RETRY, 5000)
    assert decision.execute is False
    assert "Run-total" in decision.reason


def test_seed_idempotency_is_idempotent():
    gate = Gate()
    gate.seed_from_checkpoint(1000, {("sub_a", "delayed_retry")})
    gate.seed_from_checkpoint(2000, {("sub_b", "immediate_retry")})
    assert ("sub_a", "delayed_retry") in gate._seen
    assert ("sub_b", "immediate_retry") in gate._seen
    assert gate._run_total_paise == 2000


def test_checkpoint_file_integrity():
    checkpoint = Path(__file__).parent.parent / "logs" / "results_checkpoint.jsonl"
    if not checkpoint.exists():
        import pytest

        pytest.skip("no checkpoint file")
    results = _load_checkpoint(checkpoint)
    assert len(results) > 0
    for r in results:
        assert "subscription_id" in r
        assert "final_action" in r
        assert "amount_paise" in r
        assert isinstance(r["amount_paise"], int)
