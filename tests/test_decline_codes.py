"""
Unit tests for decline_codes.py - the deterministic policy table the gate
checks every LLM proposal against. If this table is wrong, the gate
enforces the wrong thing with total confidence, which is worse than no
gate at all.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from decline_codes import DECLINE_CODES, POLICY_PATH, RecoveryAction, _load_decline_codes, get_decline_code


def test_every_code_has_a_valid_recovery_action():
    for code, info in DECLINE_CODES.items():
        assert isinstance(info.allowed_action, RecoveryAction)


def test_fraud_code_is_never_a_retry_action():
    fraud = get_decline_code("payment_risk_check_failed")
    assert fraud.allowed_action == RecoveryAction.NO_ACTION_FRAUD
    assert fraud.simulated_success_rate == 0.0


def test_no_action_policies_have_zero_simulated_success_rate():
    for code, info in DECLINE_CODES.items():
        if info.allowed_action in (RecoveryAction.NO_ACTION_FRAUD, RecoveryAction.NO_ACTION_UNRECOVERABLE):
            assert info.simulated_success_rate == 0.0, f"{code} is a no-action policy but has a nonzero success rate"


def test_actionable_policies_have_a_plausible_success_rate():
    for code, info in DECLINE_CODES.items():
        if info.allowed_action not in (RecoveryAction.NO_ACTION_FRAUD, RecoveryAction.NO_ACTION_UNRECOVERABLE):
            assert 0.0 < info.simulated_success_rate <= 1.0, f"{code} has an implausible success rate"


def test_every_recovery_action_has_a_plain_english_glossary_entry():
    # POLICY_DASHBOARD.html (generate_policy_dashboard.py) and the config
    # file's own "_action_glossary" exist so a merchant never has to open
    # a .py file to know what an allowed_action actually does. If a new
    # RecoveryAction is ever added without a matching glossary entry, that
    # promise silently breaks - catch it here instead of a merchant finding
    # a blank explanation.
    raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    glossary = raw.get("_action_glossary", {})
    for action in RecoveryAction:
        assert action.value in glossary, f"{action.value} has no _action_glossary entry in {POLICY_PATH.name}"
        assert glossary[action.value].strip(), f"{action.value}'s _action_glossary entry is empty"


def test_unknown_code_raises_keyerror():
    try:
        get_decline_code("not_a_real_code")
        assert False, "expected KeyError"
    except KeyError:
        pass


def _write_and_load(entry: dict):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"bad_code": entry}, f)
        path = Path(f.name)
    try:
        return _load_decline_codes(path)
    finally:
        path.unlink()


def test_config_typo_in_allowed_action_fails_loudly():
    # A merchant editing config/decline_policy.json by hand can typo an
    # enum value - this must raise immediately, never silently load a
    # policy the gate then enforces with total confidence.
    try:
        _write_and_load({
            "description": "test",
            "source": "customer",
            "allowed_action": "retry_whenever_i_feel_like_it",
            "simulated_success_rate": 0.5,
        })
        assert False, "expected ValueError for invalid allowed_action"
    except ValueError as e:
        assert "allowed_action" in str(e)


def test_config_typo_in_source_fails_loudly():
    try:
        _write_and_load({
            "description": "test",
            "source": "the_moon",
            "allowed_action": "delayed_retry",
            "simulated_success_rate": 0.5,
        })
        assert False, "expected ValueError for invalid source"
    except ValueError as e:
        assert "source" in str(e)


def test_config_out_of_range_success_rate_fails_loudly():
    # A merchant typo like "5.0" instead of "0.5" would otherwise silently
    # corrupt the synthetic simulator (or, worse, any future real-metrics
    # use) rather than fail at load time like allowed_action/source already do.
    try:
        _write_and_load({
            "description": "test",
            "source": "customer",
            "allowed_action": "delayed_retry",
            "simulated_success_rate": 5.0,
        })
        assert False, "expected ValueError for out-of-range simulated_success_rate"
    except ValueError as e:
        assert "simulated_success_rate" in str(e)


def test_config_negative_success_rate_fails_loudly():
    try:
        _write_and_load({
            "description": "test",
            "source": "customer",
            "allowed_action": "delayed_retry",
            "simulated_success_rate": -0.1,
        })
        assert False, "expected ValueError for negative simulated_success_rate"
    except ValueError as e:
        assert "simulated_success_rate" in str(e)


def test_config_non_numeric_success_rate_fails_loudly():
    try:
        _write_and_load({
            "description": "test",
            "source": "customer",
            "allowed_action": "delayed_retry",
            "simulated_success_rate": "high",
        })
        assert False, "expected ValueError for non-numeric simulated_success_rate"
    except ValueError as e:
        assert "simulated_success_rate" in str(e)


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
