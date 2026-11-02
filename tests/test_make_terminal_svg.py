"""scripts/make_terminal_svg.py renders real command output as an SVG."""

import importlib.util
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "make_terminal_svg", REPO / "scripts" / "make_terminal_svg.py"
)
mts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mts)


def test_clean_strips_temp_paths_and_truncates():
    text = (
        "Wrote C:\\Users\\x\\AppData\\Local\\Temp\\failsafe_demo_ab12\\REPORT.html\n\tindented  \n"
        + "\n".join(f"l{i}" for i in range(20))
        + "\n\n"
    )
    lines = mts.clean(text, max_lines=6)
    assert lines[0] == "Wrote $TMP\\REPORT.html"
    assert len(lines) == 6 and any(ln.startswith("... (") for ln in lines) and lines[-1] == "l19"
    assert mts.clean("/tmp/failsafe_demo_x1/a\n") == ["$TMP/a"]


def test_colour_rules():
    assert mts.colour("$ python x")[0] == ("$ ", mts.GREEN)
    assert mts.colour("== Summary ==")[0][1] == mts.VIOLET
    assert mts.colour("... (3 lines)")[0][1] == mts.DIM
    assert mts.colour("sub_1 code -> no_action_unrecoverable")[2][1] == mts.AMBER
    assert mts.colour("sub_1 code -> delayed_retry")[2][1] == mts.GREEN
    assert mts.colour("replay: 12/12 decisions reproduce")[0][1] == mts.GREEN
    assert mts.colour("replay: 11/12 decisions reproduce")[0][1] == mts.RED
    assert mts.colour("LLM proposals overridden: 3")[0][1] == mts.AMBER
    assert mts.colour("plain")[0][1] == mts.FG


def test_render_is_valid_svg_and_escapes():
    svg = mts.render(["$ echo <hi> & bye", "x" * 200], "t<1>", width_chars=80)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert "&lt;hi&gt; &amp; bye" in svg and "…" in svg


def test_main_from_file_and_command(tmp_path, capsys):
    src = tmp_path / "in.txt"
    src.write_text("== hello ==\nline\n", encoding="utf-8")
    out = tmp_path / "o.svg"
    assert (
        mts.main(["--from-file", str(src), "--out", str(out), "python -c \"print('cmd ran')\""])
        == 0
    )
    svg = out.read_text(encoding="utf-8")
    assert "hello" in svg and "cmd ran" in svg and ">python -c" in svg
    assert "Wrote" in capsys.readouterr().out


def test_run_includes_stderr_on_failure():
    assert "boom" in mts.run("python -c \"import sys; sys.exit('boom')\"", sys.executable)


def test_main_requires_input(tmp_path):
    with pytest.raises(SystemExit):
        mts.main(["--out", str(tmp_path / "x.svg")])
