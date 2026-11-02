"""
FailSafe - a gated, audited revenue-recovery agent for Razorpay.

The LLM proposes; deterministic code decides. Every proposal passes through
a policy gate before anything touches Razorpay, and every step is audited.

Layout: this package contains all pipeline code. Datasets live in
``data/``, merchant-editable policy tables in ``config/``, audit trails in
``logs/`` - all anchored by :mod:`failsafe.paths`.

Run any pipeline stage as a module, e.g.::

    python -m failsafe generate-data
    python -m failsafe agent --inject-failure llm_parse_failure
    python -m failsafe integrated [N]
"""

from importlib import metadata as _metadata
from pathlib import Path as _Path

# Keep in sync with pyproject.toml; used only when neither source below exists.
_PYPROJECT_VERSION = "1.1.0"


def _resolve_version(checkout_root: _Path) -> str:
    """Version of the code actually being imported.

    1. A source checkout (pyproject.toml next to src/): read it there. This
       beats installed metadata, which can belong to a stale editable install
       of a different checkout.
    2. Otherwise the installed distribution's metadata (importlib.metadata).
    3. Otherwise the pinned fallback above.
    """
    pyproject = checkout_root / "pyproject.toml"
    if pyproject.is_file():
        import tomllib

        project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
        if project.get("name") == "failsafe" and project.get("version"):
            return str(project["version"])
    try:
        return _metadata.version("failsafe")
    except _metadata.PackageNotFoundError:
        return _PYPROJECT_VERSION


__version__ = _resolve_version(_Path(__file__).resolve().parent.parent.parent)
