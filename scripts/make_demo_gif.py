"""
Build docs/screenshots/offline-demo.gif: an animated terminal recording of
`scripts/offline_demo.py`, ending on the REPORT.html it generates.

Nothing is faked: the script runs the offline demo (no Ollama, no Razorpay
keys), renders its real stdout line by line with Pillow, then screenshots the
demo's own REPORT.html with an isolated headless Chrome (throwaway profile).
Temp paths in the output are shortened to <tmp> so the GIF holds no local
user paths. The grey "gate overrode LLM" / "LLM proposal kept" tags are
annotations added by this script (the stand-in LLM always proposes
immediate_retry, so any other final action is a gate override).

Requires Pillow (not a project dependency) and, for the final report frame,
Chrome/Chromium. Without a browser the GIF is terminal-only.

Usage:
    python scripts/make_demo_gif.py --python .venv/Scripts/python.exe
    python scripts/make_demo_gif.py --chrome "C:/Program Files/Google/Chrome/Application/chrome.exe"
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "screenshots" / "offline-demo.gif"

WIDTH = 900
PAD = 18
LINE_H = 19
FONT_SIZE = 14
BG = (22, 24, 29)
FG = (205, 210, 218)
DIM = (120, 128, 140)
GREEN = (120, 200, 130)
YELLOW = (230, 190, 90)
CYAN = (110, 190, 230)
PROMPT = "$ python scripts/offline_demo.py --n 12"
ROW = re.compile(r"^(sub_\w+)\s+(\S+)\s+-> (\S+)$")

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "google-chrome",
    "chromium",
    "chromium-browser",
]


def load_font(bold: bool = False) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    names = (
        ["consolab.ttf", "DejaVuSansMono-Bold.ttf"]
        if bold
        else ["consola.ttf", "DejaVuSansMono.ttf"]
    )
    for name in names:
        for base in (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu")):
            if (base / name).exists():
                return ImageFont.truetype(str(base / name), FONT_SIZE)
    return ImageFont.load_default()


def run_demo(python: str) -> tuple[list[str], Path | None]:
    proc = subprocess.run(
        [
            str(Path(python).resolve()) if Path(python).exists() else python,
            str(REPO / "scripts" / "offline_demo.py"),
            "--n",
            "12",
            "--keep",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        cwd=REPO,
    )
    lines = proc.stdout.splitlines()
    kept = next((ln for ln in lines if ln.startswith("Outputs kept in ")), None)
    out_dir = Path(kept[len("Outputs kept in ") :].split("  (")[0]) if kept else None
    clean = []
    for ln in lines:
        if ln.startswith("Outputs kept in "):
            continue
        ln = re.sub(r"[A-Za-z]:\\[^ ]*failsafe_demo_\w+\\", "<tmp>/", ln)
        ln = re.sub(r"/tmp/failsafe_demo_\w+/", "<tmp>/", ln)
        clean.append(ln)
    while clean and not clean[-1].strip():
        clean.pop()
    return clean, out_dir


def colour_spans(line: str) -> list[tuple[str, tuple[int, int, int]]]:
    """Split a stdout line into coloured spans (overrides in yellow)."""
    m = ROW.match(line)
    if m:
        sub, code, action = m.groups()
        left = line[: line.index("->")]
        overridden = action != "immediate_retry"  # the stand-in always proposes immediate_retry
        tag = "  gate overrode LLM" if overridden else "  LLM proposal kept"
        return [
            (left[: len(sub)], DIM),
            (left[len(sub) :], FG),
            ("-> ", DIM),
            (action, YELLOW if overridden else GREEN),
            (tag, DIM),
        ]
    if line.startswith("=="):
        return [(line, CYAN)]
    if line.startswith("LLM proposals overridden"):
        return [(line, YELLOW)]
    return [(line, FG)]


def render(lines: list[str], height: int, font, bold, cursor: bool = False) -> Image.Image:
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse((PAD + i * 20, 10, PAD + i * 20 + 11, 21), fill=c)
    d.text(
        (WIDTH // 2, 16),
        "FailSafe - offline demo (no Ollama, no keys)",
        fill=DIM,
        font=font,
        anchor="mm",
    )
    y = 38
    for idx, line in enumerate(lines):
        x = PAD
        spans = [(line, GREEN)] if idx == 0 and line.startswith("$") else colour_spans(line)
        for text, col in spans:
            f = bold if col in (CYAN, YELLOW) else font
            d.text((x, y), text, fill=col, font=f)
            x += d.textlength(text, font=f)
        y += LINE_H
    if cursor:
        last = lines[-1] if lines else ""
        cx = PAD + d.textlength(last, font=font)
        d.rectangle((cx + 2, y - LINE_H + 2, cx + 10, y - 3), fill=FG)
    return img


def find_chrome(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for c in CHROME_CANDIDATES:
        if Path(c).exists() or shutil.which(c):
            return c
    return None


def screenshot_report(chrome: str, report: Path, work: Path) -> Image.Image | None:
    png = work / "report.png"
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={work / 'profile'}",
        "--hide-scrollbars",
        "--window-size=1280,900",
        f"--screenshot={png}",
        report.resolve().as_uri(),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not png.exists():
        return None
    shot = Image.open(png).convert("RGB")
    return shot.resize((WIDTH, round(shot.height * WIDTH / shot.width)), Image.LANCZOS)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the README offline-demo GIF")
    ap.add_argument("--chrome", help="path to a Chrome/Chromium binary")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument(
        "--python",
        default=sys.executable,
        help="interpreter with the project deps for the demo run (e.g. .venv python)",
    )
    args = ap.parse_args()

    font, bold = load_font(), load_font(bold=True)
    lines, demo_dir = run_demo(args.python)
    all_lines = [PROMPT, *lines]
    height = 38 + LINE_H * len(all_lines) + PAD

    frames: list[Image.Image] = []
    durations: list[int] = []
    for i in range(2, len(PROMPT) + 1, 4):  # type the command
        frames.append(render([PROMPT[:i]], height, font, bold, cursor=True))
        durations.append(60)
    durations[-1] = 500
    for n in range(2, len(all_lines) + 1):  # stream the real output
        frames.append(render(all_lines[:n], height, font, bold))
        is_row = bool(ROW.match(all_lines[n - 1]))
        durations.append(220 if is_row else 120)
    durations[-1] = 3500

    work = Path(tempfile.mkdtemp(prefix="failsafe_gif_"))
    try:
        chrome = find_chrome(args.chrome)
        report = demo_dir / "REPORT.html" if demo_dir else None
        shot = (
            screenshot_report(chrome, report, work)
            if chrome and report and report.exists()
            else None
        )
        if shot is not None:
            frame = Image.new("RGB", (WIDTH, height), BG)
            frame.paste(shot.crop((0, 0, WIDTH, min(height, shot.height))), (0, 0))
            frames.append(frame)
            durations.append(4000)
        else:
            print("note: no browser screenshot; GIF is terminal-only")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if demo_dir:
            shutil.rmtree(demo_dir, ignore_errors=True)

    pal = [f.convert("P", palette=Image.ADAPTIVE, colors=64) for f in frames]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    pal[0].save(
        args.out, save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True
    )
    print(f"Wrote {args.out} ({len(frames)} frames, {args.out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
