"""
FailSafe console: a small, read-only web server for the decision trace, the
audit report, the merchant policy view and a replay API.

Pages are regenerated from the audit log whenever it changes, into an output
directory (never the repo's committed HTML files). Standard library only; no
LLM, no Razorpay, no writes to the audit log. Put it behind your own auth
before exposing it beyond localhost: it shows record ids and amounts.

    python -m failsafe.console                 # http://127.0.0.1:8080
    python -m failsafe console --host 0.0.0.0 --port 9000
    PORT=9000 HOST=0.0.0.0 python -m failsafe.console

Routes:
    /               Decision Trace (TRACE.html)
    /report         audit report (REPORT.html)
    /policy         merchant policy view (POLICY_DASHBOARD.html)
    /api/replay     JSON: replay of the audit log under the live policy
    /api/policy     JSON: the live policy, with decision counts per code
    POST /api/whatif  {"overrides": {"<code>": "<action>"}} -> replay under the edited policy
    /policy/edit    what-if policy editor (nothing is saved server-side)
    /api/record/<id>  Markdown incident report for one record
    /healthz        JSON liveness + what is being served

Environment (all optional):
    FAILSAFE_AUDIT_LOG  audit log to serve        (default logs/audit_log.jsonl)
    FAILSAFE_POLICY     decline policy to replay  (default config/decline_policy.json)
    FAILSAFE_DATA       dataset for old-log inputs (default data/halted_subscriptions.json)
    FAILSAFE_OUT_DIR    where pages are generated (default: a temp dir)
    HOST / PORT       bind address               (default 127.0.0.1 / 8080)
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from failsafe import decision_trace, generate_policy_dashboard, generate_report, replay
from failsafe.decline_codes import RecoveryAction

MAX_BODY_BYTES = 64 * 1024


class BadRequest(ValueError):
    """A client error in a what-if request (-> HTTP 400)."""


@dataclass
class Settings:
    audit: Path
    policy: Path
    data: Path
    out: Path
    host: str
    port: int

    @classmethod
    def from_env(cls, env: dict | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        out = env.get("FAILSAFE_OUT_DIR") or tempfile.mkdtemp(prefix="failsafe_console_")
        try:
            port = int(env.get("PORT", "8080"))
        except ValueError as e:
            raise SystemExit(f"PORT must be a number, got {env.get('PORT')!r}") from e
        return cls(
            audit=Path(env.get("FAILSAFE_AUDIT_LOG") or replay.DEFAULT_AUDIT),
            policy=Path(env.get("FAILSAFE_POLICY") or replay.DEFAULT_POLICY),
            data=Path(env.get("FAILSAFE_DATA") or replay.DEFAULT_DATA),
            out=Path(out),
            host=env.get("HOST", "127.0.0.1"),
            port=port,
        )


class Pages:
    """Regenerates the HTML pages when the audit log or policy changes."""

    def __init__(self, s: Settings):
        self.s = s
        self.s.out.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._stamp: tuple | None = None

    def _current_stamp(self) -> tuple:
        def st(p: Path):
            try:
                m = p.stat()
                return (m.st_mtime_ns, m.st_size)
            except OSError:
                return None

        return (st(self.s.audit), st(self.s.policy))

    def refresh(self) -> None:
        with self._lock:
            stamp = self._current_stamp()
            if stamp == self._stamp:
                return
            if not self.s.audit.exists():
                raise FileNotFoundError(f"audit log not found: {self.s.audit}")
            decision_trace.build(
                self.s.audit, self.s.out / "TRACE.html", self.s.policy, self.s.data
            )
            # The report generators read module-level paths; point them at our
            # inputs/outputs (the console is their only user in this process).
            generate_report.AUDIT_PATH = self.s.audit
            generate_report.REPORT_PATH = self.s.out / "REPORT.html"
            generate_report.build_report()
            generate_policy_dashboard.POLICY_PATH = self.s.policy
            generate_policy_dashboard.DASHBOARD_PATH = self.s.out / "POLICY_DASHBOARD.html"
            generate_policy_dashboard.build_dashboard()
            self._stamp = stamp

    def page(self, name: str) -> bytes:
        self.refresh()
        return (self.s.out / name).read_bytes()

    def _replay(self, policy) -> replay.ReplayReport:
        return replay.replay(
            replay.load_events(self.s.audit),
            policy,
            replay.load_dataset_index(self.s.data),
            policy_path=self.s.policy.name,
            audit_path=self.s.audit.name,
        )

    def replay_json(self) -> dict:
        rep = self._replay(replay.load_decline_codes(self.s.policy))
        return {
            "audit_log": self.s.audit.name,
            "policy": self.s.policy.name,
            **replay.summary_dict(rep),
        }

    def policy_json(self) -> dict:
        """The live policy, for the what-if editor: each code with its action
        and how many recorded decisions it covers."""
        raw = json.loads(self.s.policy.read_text(encoding="utf-8"))
        table = replay.load_decline_codes(self.s.policy)
        counts = Counter(o.decline_code for o in self._replay(table).outcomes)
        return {
            "policy": self.s.policy.name,
            "actions": [a.value for a in RecoveryAction],
            "glossary": raw.get("_action_glossary", {}),
            "codes": [
                {
                    "code": c,
                    "action": d.allowed_action.value,
                    "source": d.source.value,
                    "description": d.description,
                    "decisions": counts.get(c, 0),
                }
                for c, d in table.items()
            ],
        }

    def whatif(self, body: bytes) -> dict:
        """Replay under the live policy with some codes' actions changed.
        Nothing is written: the candidate exists only for this request."""
        try:
            parsed = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise BadRequest(
                f'body must be JSON like {{"overrides": {{"<code>": "<action>"}}}} ({e})'
            ) from e
        overrides = parsed.get("overrides", {}) if isinstance(parsed, dict) else None
        if not isinstance(overrides, dict):
            raise BadRequest("overrides must be an object of decline_code -> action")
        table = replay.load_decline_codes(self.s.policy)
        try:
            candidate = replay.apply_overrides(table, overrides)
        except ValueError as e:
            raise BadRequest(str(e)) from e
        rep = self._replay(candidate)
        rep.policy_path = f"{self.s.policy.name} + {len(overrides)} edit(s)"
        # The full candidate file (comments and glossary kept), ready to save as a PR.
        raw = json.loads(self.s.policy.read_text(encoding="utf-8"))
        for code, action in overrides.items():
            raw[code] = {**raw[code], "allowed_action": action}
        return {
            "policy": rep.policy_path,
            "overrides": overrides,
            **replay.summary_dict(rep),
            "markdown": replay.render_summary(rep, title="FailSafe policy what-if"),
            "candidate": raw,
        }

    def incident(self, record_id: str) -> str:
        events = replay.load_events(self.s.audit)
        rep = replay.replay(
            events,
            replay.load_decline_codes(self.s.policy),
            replay.load_dataset_index(self.s.data),
            policy_path=self.s.policy.name,
            audit_path=self.s.audit.name,
        )
        return replay.incident_report(events, record_id, rep)


HTML_ROUTES = {
    "/": "TRACE.html",
    "/trace": "TRACE.html",
    "/report": "REPORT.html",
    "/policy": "POLICY_DASHBOARD.html",
    # the file names the static pages link to each other by
    "/TRACE.html": "TRACE.html",
    "/REPORT.html": "REPORT.html",
    "/POLICY_DASHBOARD.html": "POLICY_DASHBOARD.html",
}
EDITOR_PAGE = Path(__file__).parent / "assets" / "policy_editor.html"
API_ROUTES = [
    "/policy/edit",
    "/api/policy",
    "POST /api/whatif",
    "/api/replay",
    "/api/record/<id>",
    "/healthz",
]


def make_handler(pages: Pages):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FailSafe"
        sys_version = ""

        def log_message(self, format, *args):  # quiet by default; one line per request
            print(f"[console] {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, indent=2).encode(), "application/json; charset=utf-8")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            try:
                if path == "/healthz":
                    return self._json(
                        200,
                        {
                            "ok": pages.s.audit.exists(),
                            "audit_log": pages.s.audit.name,
                            "policy": pages.s.policy.name,
                        },
                    )
                if path in HTML_ROUTES:
                    return self._send(
                        200, pages.page(HTML_ROUTES[path]), "text/html; charset=utf-8"
                    )
                if path == "/api/replay":
                    return self._json(200, pages.replay_json())
                if path == "/api/policy":
                    return self._json(200, pages.policy_json())
                if path == "/policy/edit":
                    return self._send(200, EDITOR_PAGE.read_bytes(), "text/html; charset=utf-8")
                if path.startswith("/api/record/"):
                    rid = unquote(path[len("/api/record/") :])
                    if not rid or "/" in rid:
                        return self._json(400, {"error": "record id required"})
                    return self._send(
                        200, pages.incident(rid).encode("utf-8"), "text/markdown; charset=utf-8"
                    )
                return self._json(
                    404, {"error": "not found", "routes": sorted(HTML_ROUTES) + API_ROUTES}
                )
            except FileNotFoundError as e:
                return self._json(503, {"error": str(e)})
            except (ValueError, KeyError) as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path != "/api/whatif":
                return self._json(404, {"error": "not found", "routes": API_ROUTES})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json(400, {"error": "bad Content-Length"})
            if length > MAX_BODY_BYTES:
                return self._json(413, {"error": f"body over {MAX_BODY_BYTES} bytes"})
            body = self.rfile.read(length) if length > 0 else b""
            try:
                return self._json(200, pages.whatif(body))
            except BadRequest as e:
                return self._json(400, {"error": str(e)})
            except FileNotFoundError as e:
                return self._json(503, {"error": str(e)})
            except (ValueError, KeyError) as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    return Handler


def make_server(s: Settings) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((s.host, s.port), make_handler(Pages(s)))


def build_parser(env: dict | None = None) -> argparse.ArgumentParser:
    env = dict(os.environ if env is None else env)
    parser = argparse.ArgumentParser(
        prog="python -m failsafe console",
        description="Serve the read-only FailSafe console (decision trace, report, "
        "policy view, replay API).",
        epilog="Environment: FAILSAFE_AUDIT_LOG, FAILSAFE_POLICY, FAILSAFE_DATA, "
        "FAILSAFE_OUT_DIR, HOST, PORT (flags override HOST/PORT).",
    )
    parser.add_argument(
        "--host",
        default=env.get("HOST", "127.0.0.1"),
        help="bind address (default: $HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="port (default: $PORT or 8080; 0 picks a free port)",
    )
    return parser


def main(argv: Sequence[str] | None = None, env: dict | None = None) -> int:
    args = build_parser(env).parse_args(argv)
    s = Settings.from_env(env)
    s.host = args.host
    if args.port is not None:
        s.port = args.port
    httpd = make_server(s)
    host, port = str(httpd.server_address[0]), httpd.server_address[1]
    print(f"FailSafe console on http://{host}:{port}  (audit log: {s.audit}, pages in {s.out})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
