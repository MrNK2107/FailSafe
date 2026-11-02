"""Packaging metadata, version source and the FAILSAFE_HOME / FAILSAFE_RESULTS_DIR
path overrides (results must be writable by a non-root container user)."""

import importlib
import tomllib
from pathlib import Path

import pytest

import failsafe
from failsafe import paths

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_failsafe_agent_console_script_is_declared():
    scripts = PYPROJECT["project"]["scripts"]
    assert scripts["failsafe-agent"] == "failsafe.agent:main"
    assert scripts["failsafe-console"] == "failsafe.console:main"


def test_version_matches_pyproject():
    assert failsafe.__version__ == PYPROJECT["project"]["version"]


def test_version_comes_from_metadata_outside_a_checkout(monkeypatch, tmp_path):
    import importlib.metadata as md

    monkeypatch.setattr(md, "version", lambda name: "9.9.9")
    assert failsafe._resolve_version(tmp_path) == "9.9.9"


def test_version_falls_back_to_pinned_pyproject_version(monkeypatch, tmp_path):
    import importlib.metadata as md

    def missing(name):
        raise md.PackageNotFoundError(name)

    monkeypatch.setattr(md, "version", missing)
    assert failsafe._resolve_version(tmp_path) == PYPROJECT["project"]["version"]
    assert failsafe._PYPROJECT_VERSION == PYPROJECT["project"]["version"]


@pytest.fixture
def reload_paths(monkeypatch):
    def _reload(**env):
        for k in ("FAILSAFE_HOME", "FAILSAFE_RESULTS_DIR"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return importlib.reload(paths)

    yield _reload
    monkeypatch.undo()
    importlib.reload(paths)


def test_defaults_are_the_source_checkout(reload_paths):
    p = reload_paths()
    assert p.PROJECT_ROOT == ROOT
    assert p.RESULTS_DIR == ROOT
    assert p.LOGS_DIR == ROOT / "logs" and p.CONFIG_DIR == ROOT / "config"


def test_failsafe_home_relocates_every_dir(reload_paths, tmp_path):
    p = reload_paths(FAILSAFE_HOME=str(tmp_path))
    assert p.PROJECT_ROOT == tmp_path.resolve()
    assert p.DATA_DIR == tmp_path.resolve() / "data"
    assert p.RESULTS_DIR == tmp_path.resolve()


def test_results_dir_override_is_independent(reload_paths, tmp_path):
    p = reload_paths(FAILSAFE_RESULTS_DIR=str(tmp_path / "out"))
    assert p.RESULTS_DIR == (tmp_path / "out").resolve()
    assert p.PROJECT_ROOT == ROOT


def test_results_writers_use_results_dir():
    """Every module that writes a *RESULTS*.md / report page targets RESULTS_DIR,
    never the (root-owned in the image) project root."""
    src = ROOT / "src" / "failsafe"
    offenders = [
        f.name
        for f in src.glob("*.py")
        if 'PROJECT_ROOT / "' in f.read_text(encoding="utf-8") and f.name != "paths.py"
    ]
    assert offenders == []


def test_dockerfile_points_results_under_writable_logs():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FAILSAFE_RESULTS_DIR=/app/logs/results" in docker
    assert "mkdir -p /app/logs/results" in docker
    assert "chown -R failsafe:failsafe /app/logs" in docker


def test_policy_check_workflow_watches_policy_loader_and_paths():
    wf = (ROOT / ".github" / "workflows" / "policy-check.yml").read_text(encoding="utf-8")
    assert '"src/failsafe/policy_loader.py"' in wf
    assert '"src/failsafe/paths.py"' in wf


def test_env_example_documents_every_runtime_knob():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    deploy = (ROOT / "docs" / "DEPLOY.md").read_text(encoding="utf-8")
    for var in (
        "FAILSAFE_INVALID_ACTION_THRESHOLD",
        "RAZORPAY_PARTNER_LINKED_ACCOUNT_ID",
        "FAILSAFE_RESULTS_DIR",
        "FAILSAFE_HOME",
    ):
        assert var in example, var
        assert var in deploy, var
