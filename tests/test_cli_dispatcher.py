"""`python -m failsafe <command>` forwards to each module's own CLI."""

import sys

from failsafe import __main__ as cli


def test_every_command_maps_to_an_importable_module():
    import importlib.util

    for module, _help in cli.COMMANDS.values():
        assert importlib.util.find_spec(module) is not None, module


def test_no_args_prints_usage_and_fails(capsys):
    assert cli.main([]) == 2
    assert "usage: python -m failsafe" in capsys.readouterr().out


def test_help_lists_replay_trace_console_commands(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for cmd in ("replay", "trace", "console", "agent"):
        assert cmd in out


def test_unknown_command(capsys):
    assert cli.main(["nope"]) == 2
    assert "unknown command: nope" in capsys.readouterr().err


def test_forwards_args_and_exit_code(capsys):
    argv_before = list(sys.argv)
    assert cli.main(["replay", "--help"]) == 0
    assert "--fail-on-change" in capsys.readouterr().out
    assert sys.argv == argv_before


def test_replay_reproduces_committed_log(capsys):
    assert cli.main(["replay", "--fail-on-change"]) == 0
