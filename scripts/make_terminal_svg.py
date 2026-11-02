"""
Render real terminal output as a static SVG "screenshot" in the FailSafe style.

Runs each command (from the repo root, no Ollama, no keys needed for the
commands used in the README), captures stdout, and draws it in a terminal
window. Text is kept as text, so the SVG stays small, sharp and searchable.

    python scripts/make_terminal_svg.py --out docs/screenshots/terminal-demo.svg \\
        "python scripts/offline_demo.py --n 12" "python -m failsafe.replay"

    python scripts/make_terminal_svg.py --from-file out.txt --title "replay" --out x.svg
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

BG = "#0D1321"
FG = "#D8DEE9"
DIM = "#7B8496"
GREEN = "#34D399"
AMBER = "#FBBF24"
VIOLET = "#A78BFA"
RED = "#F87171"
CHAR_W = 7.8  # px per character at 13px monospace
LINE_H = 18


def run(cmd: str, python: str) -> str:
    argv = shlex.split(cmd, posix=True)
    if argv and argv[0] == "python":
        argv[0] = python
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "OLLAMA_HOST": os.environ.get("OLLAMA_HOST", "http://127.0.0.1:9"),
    }
    p = subprocess.run(
        argv, cwd=REPO, capture_output=True, text=True, encoding="utf-8", env=env, timeout=300
    )
    return p.stdout + (p.stderr if p.returncode else "")


_TEMP_PATH = re.compile(r"[A-Za-z]:\\[^\s]*?failsafe_demo_[a-z0-9_]+|/tmp/failsafe_demo_[a-z0-9_]+")


def clean(text: str, max_lines: int | None = None) -> list[str]:
    """Normalise output for display: strip temp paths, tabs and trailing space."""
    text = _TEMP_PATH.sub("$TMP", text)
    lines = [ln.rstrip().replace("\t", "    ") for ln in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    if max_lines and len(lines) > max_lines:
        keep = max_lines - 1
        head = keep // 2
        lines = lines[:head] + [f"... ({len(lines) - keep} lines)"] + lines[-(keep - head) :]
    return lines


def colour(line: str) -> list[tuple[str, str]]:
    """Split a line into (text, colour) spans."""
    if line.startswith("$ "):
        return [("$ ", GREEN), (line[2:], FG)]
    if line.startswith("==") or line.startswith("## ") or line.startswith("# "):
        return [(line, VIOLET)]
    if line.startswith("... ("):
        return [(line, DIM)]
    if "->" in line:
        left, right = line.split("->", 1)
        tone = AMBER if "no_action" in right else GREEN
        return [(left, FG), ("->", DIM), (right, tone)]
    m = re.search(r"(\d+)/(\d+)", line)
    if m and ("reproduce" in line or "Reproduced" in line):
        return [(line, GREEN if m.group(1) == m.group(2) else RED)]
    if "overridden" in line.lower() or "Would change" in line:
        return [(line, AMBER)]
    return [(line, FG)]


def render(lines: list[str], title: str, width_chars: int | None = None) -> str:
    cols = max([len(ln) for ln in lines] + [60])
    cols = min(cols, width_chars or 120)
    w = int(cols * CHAR_W) + 48
    h = len(lines) * LINE_H + 72
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{html.escape(title)}">',
        f'<rect width="{w}" height="{h}" rx="12" fill="{BG}"/>',
        f'<rect width="{w}" height="34" rx="12" fill="#1B2333"/><rect y="22" width="{w}" height="12" fill="#1B2333"/>',
        '<circle cx="20" cy="17" r="5.5" fill="#F87171"/><circle cx="38" cy="17" r="5.5" fill="#FBBF24"/><circle cx="56" cy="17" r="5.5" fill="#34D399"/>',
        f'<text x="{w / 2:.0f}" y="21.5" text-anchor="middle" font-family="ui-sans-serif, Segoe UI, sans-serif" font-size="12" fill="{DIM}">{html.escape(title)}</text>',
        '<g font-family="ui-monospace, JetBrains Mono, SF Mono, Consolas, monospace" font-size="13" xml:space="preserve" style="white-space:pre">',
    ]
    for i, ln in enumerate(lines):
        if len(ln) > cols:
            ln = ln[: cols - 1] + "…"
        y = 58 + i * LINE_H
        spans = "".join(f'<tspan fill="{c}">{html.escape(t)}</tspan>' for t, c in colour(ln) if t)
        out.append(f'<text x="24" y="{y}">{spans}</text>')
    out.append("</g></svg>")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render terminal output as an SVG.")
    ap.add_argument("commands", nargs="*", help="commands to run from the repo root")
    ap.add_argument(
        "--from-file", type=Path, help="render this text file instead of running commands"
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--title", default="failsafe - offline demo")
    ap.add_argument(
        "--python",
        default=sys.executable,
        help="interpreter used for commands starting with 'python'",
    )
    ap.add_argument("--max-lines", type=int, default=60)
    ap.add_argument("--width", type=int, default=110, help="max columns")
    args = ap.parse_args(argv)
    if not args.commands and not args.from_file:
        ap.error("give commands to run or --from-file")

    lines: list[str] = []
    if args.from_file:
        lines += clean(args.from_file.read_text(encoding="utf-8"), args.max_lines)
    for cmd in args.commands:
        if lines:
            lines.append("")
        lines.append(f"$ {cmd}")
        lines += clean(run(cmd, args.python), args.max_lines)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(lines, args.title, args.width), encoding="utf-8")
    print(f"Wrote {args.out} ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
