"""
Unit tests for receivables_policy.py - mirrors tests/test_decline_codes.py
and tests/test_checkout_abandonment_policy.py exactly: if this table is
wrong, or a collections team typos an edit, receivables_gate.py enforces
the wrong thing with total confidence, which is worse than no gate at
all.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from receivables_policy import (
    GATE_ONLY_ACTIONS,
    NO_ACTION_POLICY_ACTIONS,
    POLICY_PATH,
    RECEIVABLE_POLICIES,
    ReceivableAction,
    ReceivableReason,
    _load_receivables_policy,
    get_receivable_policy,
)


def test_every_reason_has_a_valid_action():
    for reason, info in RECEIVABLE_POLICIES.items():
        assert isinstance(info.allowed_action, ReceivableAction)


def test_every_receivable_reason_has_a_policy_row():
    for reason in ReceivableReason:
        assert reason.value in RECEIVABLE_POLICIES


def test_no_action_policies_have_zero_or_low_simulated_recovery_rate():
    dispute = get_receivable_policy("invoice_dispute_likely")
    assert dispute.allowed_action == ReceivableAction.NO_ACTION_NEEDS_DISPUTE_REVIEW
    assert dispute.simulated_recovery_rate == 0.0

    high_risk = get_receivable_policy("high_risk_non_payment")
    assert high_risk.allowed_action == ReceivableAction.ESCALATE_TO_MANUAL_COLLECTIONS
    assert 0.0 <= high_risk.simulated_recovery_rate <= 0.2  # low, but escalation can still recover something


def test_actionable_policies_have_a_plausible_recovery_rate():
    for reason, info in RECEIVABLE_POLICIES.items():
        if info.allowed_action not in NO_ACTION_POLICY_ACTIONS:
            assert 0.0 < info.simulated_recovery_rate <= 1.0, f"{reason} has an implausible recovery rate"


def test_every_action_has_a_plain_english_glossary_entry():
    # Mirrors test_decline_codes.py's/test_checkout_abandonment_policy.py's
    # own glossary-completeness test - if a new ReceivableAction is ever
    # added without a matching glossary entry, catch it here, not a
    # collections team finding a blank explanation.
    raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    glossary = raw.get("_action_glossary", {})
    for action in ReceivableAction:
        assert action.value in glossary, f"{action.value} has no _action_glossary entry"
        assert glossary[action.value].strip(), f"{action.value}'s glossary entry is empty"


def test_gate_only_actions_never_appear_as_a_policy_rows_allowed_action():
    # The 3 enforcement-layer-only fallbacks must never be reachable via
    # the per-reason policy table itself - they are produced only by
    # receivables_gate.py's own compliant-escalation stopping rules.
    row_actions = {info.allowed_action for info in RECEIVABLE_POLICIES.values()}
    assert row_actions.isdisjoint(GATE_ONLY_ACTIONS)


def test_no_action_policy_actions_are_a_subset_of_real_policy_rows():
    row_actions = {info.allowed_action for info in RECEIVABLE_POLICIES.values()}
    assert NO_ACTION_POLICY_ACTIONS.issubset(row_actions)


def test_unknown_reason_raises_keyerror():
    try:
        get_receivable_policy("not_a_real_reason")
        assert False, "expected KeyError"
    except KeyError:
        pass


def _write_and_load(entry: dict):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"cash_flow_delay": entry}, f)
        path = Path(f.name)
    try:
        return _load_receivables_policy(path)
    finally:
        path.unlink()


def test_config_typo_in_allowed_action_fails_loudly():
    try:
        _write_and_load({
            "description": "test",
            "allowed_action": "give_them_a_pony",
            "simulated_recovery_rate": 0.5,
        })
        assert False, "expected ValueError for invalid allowed_action"
    except ValueError as e:
        assert "allowed_action" in str(e)


def test_config_unknown_reason_key_fails_loudly():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"not_a_real_reason": {
            "description": "test", "allowed_action": "friendly_reminder", "simulated_recovery_rate": 0.5,
        }}, f)
        path = Path(f.name)
    try:
        _load_receivables_policy(path)
        assert False, "expected ValueError for unknown reason key"
    except ValueError as e:
        assert "ReceivableReason" in str(e)
    finally:
        path.unlink()


def test_config_out_of_range_recovery_rate_fails_loudly():
    try:
        _write_and_load({
            "description": "test",
            "allowed_action": "payment_plan_offer",
            "simulated_recovery_rate": 5.0,
        })
        assert False, "expected ValueError for out-of-range simulated_recovery_rate"
    except ValueError as e:
        assert "simulated_recovery_rate" in str(e)


def test_config_negative_recovery_rate_fails_loudly():
    try:
        _write_and_load({
            "description": "test",
            "allowed_action": "payment_plan_offer",
            "simulated_recovery_rate": -0.2,
        })
        assert False, "expected ValueError for negative simulated_recovery_rate"
    except ValueError as e:
        assert "simulated_recovery_rate" in str(e)


def test_config_non_numeric_recovery_rate_fails_loudly():
    try:
        _write_and_load({
            "description": "test",
            "allowed_action": "payment_plan_offer",
            "simulated_recovery_rate": "lots",
        })
        assert False, "expected ValueError for non-numeric simulated_recovery_rate"
    except ValueError as e:
        assert "simulated_recovery_rate" in str(e)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    if failures:
        raise SystemExit(1)
