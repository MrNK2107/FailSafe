"""Read-only web console (src/failsafe/console.py), served on an ephemeral port."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from failsafe import console, generate_policy_dashboard, generate_report

REPO = Path(__file__).parent.parent


@pytest.fixture
def served(tmp_path, monkeypatch):
    # The console repoints the report generators' module paths; restore them.
    for mod, names in [
        (generate_report, ("AUDIT_PATH", "REPORT_PATH")),
        (generate_policy_dashboard, ("POLICY_PATH", "DASHBOARD_PATH")),
    ]:
        for n in names:
            monkeypatch.setattr(mod, n, getattr(mod, n))
    audit = tmp_path / "audit.jsonl"
    audit.write_bytes((REPO / "logs" / "audit_log.jsonl").read_bytes())
    s = console.Settings.from_env(
        {"FAILSAFE_AUDIT_LOG": str(audit), "FAILSAFE_OUT_DIR": str(tmp_path / "out"), "PORT": "0"}
    )
    httpd = console.make_server(s)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, s, audit
    httpd.shutdown()
    httpd.server_close()


def get(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_pages_and_headers(served):
    base, s, _ = served
    code, headers, body = get(base + "/")
    assert code == 200 and b"FailSafe" in body and b"Decision Trace" in body
    assert headers["X-Content-Type-Options"] == "nosniff" and "text/html" in headers["Content-Type"]
    assert get(base + "/report")[0] == 200
    assert b"<html" in get(base + "/policy")[2].lower()
    assert get(base + "/trace?x=1#y")[0] == 200
    assert get(base + "/REPORT.html")[0] == get(base + "/POLICY_DASHBOARD.html")[0] == 200
    assert b'href="REPORT.html"' in get(base + "/TRACE.html")[2]
    code, _, body = get(base + "/", method="HEAD")
    assert code == 200 and body == b""
    # generated into the out dir, never the repo
    assert (s.out / "TRACE.html").exists() and (s.out / "REPORT.html").exists()


def test_healthz_and_replay_api(served):
    base, _, _ = served
    code, _, body = get(base + "/healthz")
    assert code == 200 and json.loads(body)["ok"] is True
    data = json.loads(get(base + "/api/replay")[2])
    assert (
        data["decisions"] == data["reproduced"] == 326
        and data["changed"] == 0
        and data["changes"] == []
    )
    assert data["llm_agreement"]["scored"] == 326


def test_record_api_and_errors(served):
    base, _, _ = served
    code, headers, body = get(base + "/api/record/sub_2c794247ffee4f")
    assert code == 200 and "markdown" in headers["Content-Type"] and b"# Incident report" in body
    assert get(base + "/api/record/")[0] == 400
    assert get(base + "/api/record/a/b")[0] == 400
    code, _, body = get(base + "/nope")
    assert code == 404 and "/api/replay" in json.loads(body)["routes"]


def test_regenerates_when_log_changes_and_503_when_missing(served):
    base, s, audit = served
    get(base + "/")
    first = (s.out / "TRACE.html").stat().st_mtime_ns
    with audit.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event_type": "run_started"}) + "\n")
    get(base + "/")
    assert (s.out / "TRACE.html").stat().st_mtime_ns != first
    audit.unlink()
    code, _, body = get(base + "/")
    assert code == 503 and "audit log not found" in json.loads(body)["error"]
    assert json.loads(get(base + "/healthz")[2])["ok"] is False


def test_bad_policy_is_a_500_not_a_crash(served, tmp_path):
    base, s, _ = served
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    s.policy = bad
    code, _, body = get(base + "/api/replay")
    assert code == 500 and "JSONDecodeError" in json.loads(body)["error"]


def test_settings_from_env_defaults_and_bad_port(monkeypatch):
    s = console.Settings.from_env({})
    assert (
        (s.host, s.port) == ("127.0.0.1", 8080)
        and s.audit.name == "audit_log.jsonl"
        and s.out.exists()
    )
    with pytest.raises(SystemExit, match="PORT"):
        console.Settings.from_env({"PORT": "eighty"})
    monkeypatch.setenv("PORT", "8123")
    assert console.Settings.from_env().port == 8123


def test_main_serves_until_interrupted(monkeypatch, tmp_path, capsys):
    class FakeServer:
        server_address = ("127.0.0.1", 1234)
        closed = False

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            FakeServer.closed = True

    monkeypatch.setattr(console, "make_server", lambda s: FakeServer())
    assert console.main([], env={"FAILSAFE_OUT_DIR": str(tmp_path)}) == 0
    assert FakeServer.closed and "http://127.0.0.1:1234" in capsys.readouterr().out


def post(url, body: bytes, headers=None):
    req = urllib.request.Request(
        url, data=body, method="POST", headers=headers or {"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_policy_api_and_editor_page(served):
    base, _, _ = served
    p = json.loads(get(base + "/api/policy")[2])
    codes = {c["code"]: c for c in p["codes"]}
    assert (
        codes["insufficient_funds"]["action"] == "delayed_retry"
        and codes["insufficient_funds"]["decisions"] > 0
    )
    assert "immediate_retry" in p["actions"] and "delayed_retry" in p["glossary"]
    code, headers, body = get(base + "/policy/edit")
    assert code == 200 and b"Policy what-if" in body and "text/html" in headers["Content-Type"]


def test_whatif_api(served):
    base, s, _ = served
    before = s.policy.read_bytes()
    code, j = post(
        base + "/api/whatif",
        json.dumps({"overrides": {"insufficient_funds": "immediate_retry"}}).encode(),
    )
    assert (
        code == 200
        and j["changed"] == 59
        and j["transitions"][0] == {"from": "delayed_retry", "to": "immediate_retry", "records": 59}
    )
    assert (
        j["candidate"]["insufficient_funds"]["allowed_action"] == "immediate_retry"
        and "_action_glossary" in j["candidate"]
    )
    assert "## FailSafe policy what-if" in j["markdown"] and j["rerouted"]["records"] == 59
    assert s.policy.read_bytes() == before  # nothing written
    code, j = post(base + "/api/whatif", b"")
    assert code == 200 and j["changed"] == 0


def test_whatif_api_rejects_bad_input(served):
    base, _, _ = served
    assert post(base + "/api/whatif", b"{nope")[0] == 400
    assert post(base + "/api/whatif", b"[]")[0] == 400
    assert post(base + "/api/whatif", json.dumps({"overrides": ["x"]}).encode())[0] == 400
    code, j = post(
        base + "/api/whatif", json.dumps({"overrides": {"made_up": "immediate_retry"}}).encode()
    )
    assert code == 400 and "unknown decline code" in j["error"]
    code, j = post(
        base + "/api/whatif", json.dumps({"overrides": {"card_expired": "refund"}}).encode()
    )
    assert code == 400 and "invalid action" in j["error"]
    assert (
        post(base + "/api/whatif", b"{}", {"Content-Length": str(console.MAX_BODY_BYTES + 1)})[0]
        == 413
    )
    assert post(base + "/api/nope", b"{}")[0] == 404
    assert post(base + "/api/whatif", b"{}", {"Content-Length": "abc"})[0] == 400


def test_whatif_api_server_errors(served, tmp_path):
    base, s, audit = served
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    s.policy = bad
    assert post(base + "/api/whatif", b"{}")[0] == 500
    s.policy = console.replay.DEFAULT_POLICY
    audit.unlink()
    assert post(base + "/api/whatif", b"{}")[0] == 503


def test_console_help_via_dispatcher_prints_usage_without_serving(monkeypatch, capsys):
    from failsafe import __main__ as cli

    monkeypatch.setattr(console, "make_server", lambda s: pytest.fail("--help must not serve"))
    assert cli.main(["console", "--help"]) == 0
    out = capsys.readouterr().out
    assert "--host" in out and "--port" in out and "FAILSAFE_OUT_DIR" in out


def test_console_flags_override_env_defaults(monkeypatch, tmp_path):
    seen = {}

    class FakeServer:
        server_address = ("0.0.0.0", 9100)

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    def fake_make_server(s):
        seen["s"] = s
        return FakeServer()

    monkeypatch.setattr(console, "make_server", fake_make_server)
    env = {"FAILSAFE_OUT_DIR": str(tmp_path), "HOST": "127.0.0.1", "PORT": "8123"}
    assert console.main(["--host", "0.0.0.0", "--port", "9100"], env=env) == 0
    assert (seen["s"].host, seen["s"].port) == ("0.0.0.0", 9100)
    assert console.main([], env=env) == 0
    assert (seen["s"].host, seen["s"].port) == ("127.0.0.1", 8123)


def test_console_rejects_non_numeric_port_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        console.main(["--port", "abc"], env={})
    assert exc.value.code == 2
