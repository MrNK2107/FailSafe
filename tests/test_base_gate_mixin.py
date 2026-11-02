"""
base_gate.IdempotencyMixin was removed: it sat after SpendingCapMixin in every
gate's bases, and SpendingCapMixin defines all three of its methods, so it was
never reached (dead code). These tests pin the behaviour every gate relies on
so the removal provably changed nothing.
"""

import pytest

from failsafe import base_gate
from failsafe.abandonment_gate import AbandonmentGate
from failsafe.base_gate import MAX_ACTION_AMOUNT_PAISE, MAX_RUN_TOTAL_PAISE, SpendingCapMixin
from failsafe.gate import Gate
from failsafe.receivables_gate import ReceivableGate

GATES = [Gate, AbandonmentGate, ReceivableGate]


def test_dead_mixin_is_gone():
    assert not hasattr(base_gate, "IdempotencyMixin")


@pytest.mark.parametrize("cls", GATES)
def test_every_gate_gets_cap_and_idempotency_from_spending_cap_mixin(cls):
    for name in ("_enforce_cap", "_check_idempotency", "_record"):
        assert getattr(cls, name) is getattr(SpendingCapMixin, name)


@pytest.mark.parametrize("cls", GATES)
def test_cap_and_idempotency_behaviour(cls):
    g = cls()
    assert g._enforce_cap(MAX_ACTION_AMOUNT_PAISE + 1)[0] is False
    assert g._enforce_cap(MAX_ACTION_AMOUNT_PAISE) == (True, "")
    key = ("r1", "act")
    assert not g._check_idempotency(key)
    g._record(key, 500)
    assert g._check_idempotency(key)
    assert g._run_total_paise == 500
    g._run_total_paise = MAX_RUN_TOTAL_PAISE
    assert g._enforce_cap(1) == (False, "Run-total spending cap would be exceeded.")


def test_check_idempotency_tolerates_missing_seen_set():
    class Bare(SpendingCapMixin):
        pass

    assert Bare()._check_idempotency(("x", "y")) is False
