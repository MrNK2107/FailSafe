"""
route_demo's command line: SIMULATE unless --live is explicit, a
configurable audit log (default stays the flagship logs/audit_log.jsonl,
but the demo now says so), and a configurable results file.

No test here reaches Razorpay: the Route order call is always replaced by a
fake that records whether simulate mode was on, and refuses to do anything
real if it was not.
"""

import json
from pathlib import Path

import pytest

from failsafe import mcp_server, razorpay_client, route_demo


@pytest.fixture(autouse=True)
def isolated_paths(monkeypatch, tmp_path):
    """Point the module defaults at tmp so no test can touch the committed
    logs/audit_log.jsonl or ROUTE_RESULTS.md, and reset the process-global
    MCP tool guard so the five demo subscription ids are not 'duplicates'."""
    monkeypatch.setattr(route_demo, "AUDIT_PATH", tmp_path / "flagship" / "audit_log.jsonl")
    monkeypatch.setattr(route_demo, "RESULTS_PATH", tmp_path / "ROUTE_RESULTS.md")
    mcp_server.reset_tool_level_guard()
    yield
    mcp_server.reset_tool_level_guard()


@pytest.fixture
def keys_configured(monkeypatch):
    """Pretend real rzp_test_ keys are configured (without any keys): every
    switch that decides simulate-vs-real says 'real'."""
    monkeypatch.setattr(razorpay_client, "keys_configured", lambda: True)


@pytest.fixture
def route_calls(monkeypatch):
    """Replace the Route order call; record the simulate flag seen per call."""
    seen: list[bool] = []

    def fake_route_order(amount_paise, subscription_id, linked_account_id, partner_share_paise):
        seen.append(mcp_server._rp.simulate)
        return {
            "id": f"order_fake_{subscription_id}",
            "simulated": mcp_server._rp.simulate,
            "amount": amount_paise,
            "transfers": [{"amount": partner_share_paise}],
        }

    monkeypatch.setattr(mcp_server._rp, "create_route_split_order", fake_route_order)
    return seen


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_default_forces_simulate_even_with_keys_configured(keys_configured, route_calls, capsys):
    assert route_demo.main([]) == 0

    assert route_calls == [True, True, True, True]  # 5 recoveries, 1 blocked by the cap
    # the switch is restored afterwards, not left forced on
    assert mcp_server._rp.simulate is False and mcp_server.is_simulated() is False
    out = capsys.readouterr().out
    assert "Mode: SIMULATE (no Razorpay calls)" in out


def test_explicit_simulate_flag_is_the_same_as_default(keys_configured, route_calls):
    assert route_demo.main(["--simulate"]) == 0
    assert route_calls and all(route_calls)


def test_live_refused_when_no_test_keys_configured(monkeypatch, route_calls, capsys):
    monkeypatch.setattr(razorpay_client, "keys_configured", lambda: False)

    assert route_demo.main(["--live"]) == 2

    err = capsys.readouterr().err
    assert "--live needs Razorpay test keys" in err and "without --live" in err
    assert route_calls == []
    assert not route_demo.AUDIT_PATH.exists()
    assert not route_demo.RESULTS_PATH.exists()


def test_live_with_keys_does_not_force_simulate(keys_configured, route_calls, capsys):
    assert route_demo.main(["--live"]) == 0

    assert route_calls == [False, False, False, False]
    assert "Mode: LIVE" in capsys.readouterr().out


def test_simulate_and_live_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as exc:
        route_demo.main(["--simulate", "--live"])
    assert exc.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_default_audit_log_is_flagship_and_says_so(route_calls, capsys):
    assert route_demo.main([]) == 0

    out = capsys.readouterr().out
    assert f"Audit log: {route_demo.AUDIT_PATH.resolve()} (appending)" in out
    assert "flagship audit log" in out and "--audit-log PATH" in out
    types = [e["event_type"] for e in read_events(route_demo.AUDIT_PATH)]
    assert types[0] == "route_demo_started" and types[-1] == "route_demo_finished"
    assert types.count("route_transfer") == 4 and types.count("route_transfer_blocked") == 1


def test_custom_audit_log_and_results_keep_flagship_untouched(route_calls, tmp_path, capsys):
    audit = tmp_path / "nested" / "route_audit.jsonl"
    results = tmp_path / "out" / "route.md"
    results.parent.mkdir()

    assert route_demo.main(["--audit-log", str(audit), "--results", str(results)]) == 0

    assert not route_demo.AUDIT_PATH.exists()
    assert not route_demo.RESULTS_PATH.exists()
    assert len(read_events(audit)) == 7  # started + 4 transfers + 1 blocked + finished
    assert "Route transfers executed: **4**" in results.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert f"Audit log: {audit.resolve()} (appending)" in out
    assert "flagship audit log" not in out
    assert f"Wrote {results}" in out


def test_audit_log_appends_rather_than_overwrites(route_calls, tmp_path):
    audit = tmp_path / "route_audit.jsonl"
    audit.write_text(json.dumps({"event_type": "earlier"}) + "\n", encoding="utf-8")

    assert route_demo.main(["--audit-log", str(audit)]) == 0

    events = read_events(audit)
    assert events[0] == {"event_type": "earlier"} and len(events) == 8


@pytest.mark.parametrize("flag", ["--audit-log", "--results"])
def test_directory_paths_are_rejected_with_a_clear_error(flag, route_calls, tmp_path, capsys):
    assert route_demo.main([flag, str(tmp_path)]) == 2
    assert f"{flag} must be a file path, not a directory" in capsys.readouterr().err
    assert route_calls == []
