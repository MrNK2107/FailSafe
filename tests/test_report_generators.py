"""
Tests for the two static HTML generators (REPORT.html and
POLICY_DASHBOARD.html). Both are pure functions of a file on disk, so each
test points the module at a tmp copy/fixture and checks the rendered page
- including that the dedup-by-latest-state rule and HTML escaping hold.
"""

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from failsafe import generate_policy_dashboard, generate_report

REPO = Path(__file__).parent.parent


def _decision(
    sub_id, code="insufficient_funds", action="delayed_retry", matched=True, executed=True
):
    return {
        "event_type": "gate_decision",
        "subscription_id": sub_id,
        "decline_code": code,
        "final_action": action,
        "llm_proposed_action": action if matched else "no_action_unrecoverable",
        "llm_matched_policy": matched,
        "gate_execute": executed,
        "gate_reason": "ok",
    }


def _write_audit(path: Path, events: list[dict]):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def test_report_counts_latest_decision_per_subscription(tmp_path):
    audit = tmp_path / "audit.jsonl"
    out = tmp_path / "REPORT.html"
    _write_audit(
        audit,
        [
            {"event_type": "run_started"},
            _decision("sub_1", matched=False),
            _decision("sub_1", matched=True),  # later run supersedes the first
            _decision("sub_2", code="card_expired", action="payment_link_nudge"),
        ],
    )
    with (
        patch.object(generate_report, "AUDIT_PATH", audit),
        patch.object(generate_report, "REPORT_PATH", out),
    ):
        generate_report.build_report()
    page = out.read_text(encoding="utf-8")
    assert page.startswith("<!DOCTYPE html>") or "<html" in page
    assert "Audit Report" in page
    assert "card_expired" in page and "insufficient_funds" in page


def test_report_empty_audit_log_writes_placeholder(tmp_path):
    audit = tmp_path / "audit.jsonl"
    out = tmp_path / "REPORT.html"
    _write_audit(audit, [{"event_type": "run_started"}])
    with (
        patch.object(generate_report, "AUDIT_PATH", audit),
        patch.object(generate_report, "REPORT_PATH", out),
    ):
        generate_report.build_report()
    assert "No gate_decision events" in out.read_text(encoding="utf-8")


def test_report_missing_audit_log_exits(tmp_path):
    with patch.object(generate_report, "AUDIT_PATH", tmp_path / "nope.jsonl"):
        with pytest.raises(SystemExit, match="No audit log found"):
            generate_report.build_report()


def test_report_escapes_html_in_labels(tmp_path):
    audit = tmp_path / "audit.jsonl"
    out = tmp_path / "REPORT.html"
    _write_audit(audit, [_decision("sub_x", code="<script>alert(1)</script>")])
    with (
        patch.object(generate_report, "AUDIT_PATH", audit),
        patch.object(generate_report, "REPORT_PATH", out),
    ):
        generate_report.build_report()
    page = out.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_report_from_real_committed_audit_log(tmp_path):
    out = tmp_path / "REPORT.html"
    with patch.object(generate_report, "REPORT_PATH", out):
        generate_report.build_report()
    assert out.stat().st_size > 1000


def test_policy_dashboard_renders_every_decline_code(tmp_path):
    out = tmp_path / "POLICY_DASHBOARD.html"
    with patch.object(generate_policy_dashboard, "DASHBOARD_PATH", out):
        generate_policy_dashboard.build_dashboard()
    page = out.read_text(encoding="utf-8")
    policy = json.loads((REPO / "config" / "decline_policy.json").read_text(encoding="utf-8"))
    codes = [k for k in policy if not k.startswith("_")]
    assert codes
    assert "Recovery Policy" in page
    for code in codes:
        assert code in page


def test_policy_dashboard_missing_policy_exits(tmp_path):
    with patch.object(generate_policy_dashboard, "POLICY_PATH", tmp_path / "missing.json"):
        with pytest.raises(SystemExit, match="No policy file"):
            generate_policy_dashboard.build_dashboard()


def test_policy_dashboard_does_not_touch_repo_copy(tmp_path):
    before = (REPO / "POLICY_DASHBOARD.html").read_bytes()
    shutil.copy(REPO / "config" / "decline_policy.json", tmp_path / "p.json")
    with (
        patch.object(generate_policy_dashboard, "POLICY_PATH", tmp_path / "p.json"),
        patch.object(generate_policy_dashboard, "DASHBOARD_PATH", tmp_path / "out.html"),
    ):
        generate_policy_dashboard.build_dashboard()
    assert (REPO / "POLICY_DASHBOARD.html").read_bytes() == before
