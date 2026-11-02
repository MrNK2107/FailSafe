"""Every src module is loaded once, under the `failsafe.` package path only.

The old `try: from src.x / except ImportError: from x` shim could load the
same file twice (as `x` and `failsafe.x`), so a test patching one copy silently
missed the other. This guards against that coming back.
"""

import importlib
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "failsafe"
MODULES = sorted(p.stem for p in SRC.glob("*.py") if p.stem not in ("__init__", "__main__"))


def test_every_src_module_imports_via_package_only():
    for name in MODULES:
        importlib.import_module(f"failsafe.{name}")
    duplicated = [name for name in MODULES if name in sys.modules]
    assert duplicated == [], f"loaded under a bare name too: {duplicated}"


def test_no_dual_import_fallback_left_in_src():
    offenders = [
        p.name
        for p in SRC.glob("*.py")
        if "except ImportError" in p.read_text(encoding="utf-8") and p.name != "llm_client.py"
    ]
    assert offenders == []
