"""
Unit tests for checkout_abandonment_policy.py - mirrors
tests/test_decline_codes.py exactly: if this table is wrong, or a
merchant typos an edit, abandonment_gate.py enforces the wrong thing with
total confidence, which is worse than no gate at all.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from checkout_abandonment_policy import (
    ABANDONMENT_POLICIES,
    GATE_ONLY_ACTIONS,
    POLICY_PATH,
    AbandonmentAction,
    AbandonmentReason,
    _load_abandonment_policy,
    get_abandonment_policy,
)


def test_every_reason_has_a_valid_action():
    for reason, info in ABANDONMENT_POLICIES.items():
        assert isinstance(info.allowed_action, AbandonmentAction)


def test_every_abandonment_reason_has_a_policy_row():
    for reason in AbandonmentReason:
        assert reason.value in ABANDONMENT_POLICIES


def test_no_action_policy_has_zero_simulated_recovery_rate():
    fraud_equivalent = get_abandonment_policy("trust_or_security_concern")
    assert fraud_equivalent.allowed_action == AbandonmentAction.NO_ACTION_RESPECT_HESITATION
    assert fraud_equivalent.simulated_recovery_rate == 0.0


def test_actionable_policies_have_a_plausible_recovery_rate():
    for reason, info in ABANDONMENT_POLICIES.items():
        if info.allowed_action != AbandonmentAction.NO_ACTION_RESPECT_HESITATION:
            assert 0.0 < info.simulated_recovery_rate <= 1.0, f"{reason} has an implausible recovery rate"


def test_every_action_has_a_plain_english_glossary_entry():
    # Mirrors test_decline_codes.py's own glossary-completeness test - if a
    # new AbandonmentAction is ever added without a matching glossary
    # entry, catch it here, not a merchant finding a blank explanation.
    raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    glossary = raw.get("_action_glossary", {})
    for action in AbandonmentAction:
        assert action.value in glossary, f"{action.value} has no _action_glossary entry"
        assert glossary[action.value].strip(), f"{action.value}'s glossary entry is empty"


def test_gate_only_actions_never_appear_as_a_policy_rows_allowed_action():
    # The 3 enforcement-layer-only fallbacks must never be reachable via
    # the per-reason policy table itself - they are produced only by
    # abandonment_gate.py's own stopping rules.
    row_actions = {info.allowed_action for info in ABANDONMENT_POLICIES.values()}
    assert row_actions.isdisjoint(GATE_ONLY_ACTIONS)


def test_unknown_reason_raises_keyerror():
    try:
        get_abandonment_policy("not_a_real_reason")
        assert False, "expected KeyError"
    except KeyError:
        pass


def _write_and_load(entry: dict):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"price_shock": entry}, f)
        path = Path(f.name)
    try:
        return _load_abandonment_policy(path)
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
            "description": "test", "allowed_action": "delayed_nudge_no_discount", "simulated_recovery_rate": 0.5,
        }}, f)
        path = Path(f.name)
    try:
        _load_abandonment_policy(path)
        assert False, "expected ValueError for unknown reason key"
    except ValueError as e:
        assert "AbandonmentReason" in str(e)
    finally:
        path.unlink()


def test_config_out_of_range_recovery_rate_fails_loudly():
    try:
        _write_and_load({
            "description": "test",
            "allowed_action": "discounted_incentive_nudge",
            "simulated_recovery_rate": 5.0,
        })
        assert False, "expected ValueError for out-of-range simulated_recovery_rate"
    except ValueError as e:
        assert "simulated_recovery_rate" in str(e)


def test_config_negative_recovery_rate_fails_loudly():
    try:
        _write_and_load({
            "description": "test",
            "allowed_action": "discounted_incentive_nudge",
            "simulated_recovery_rate": -0.2,
        })
        assert False, "expected ValueError for negative simulated_recovery_rate"
    except ValueError as e:
        assert "simulated_recovery_rate" in str(e)


def test_config_non_numeric_recovery_rate_fails_loudly():
    try:
        _write_and_load({
            "description": "test",
            "allowed_action": "discounted_incentive_nudge",
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
