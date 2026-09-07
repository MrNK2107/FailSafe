"""Test bootstrap.

``pyproject.toml`` already sets ``pythonpath = ["src"]`` so tests import the
``failsafe`` package directly. This conftest only exists as a safety net for
invocations that bypass pyproject discovery (e.g. running pytest from an
unusual CWD) - it re-adds src/ to sys.path before any test module imports
fail.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
