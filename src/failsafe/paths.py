"""
Single source of truth for every on-disk location the pipeline touches.

Every module used to compute its own paths via
``Path(__file__).parent.parent / "data" / ...`` - duplicated ~40 times and
broken the moment the layout changed. Import from here instead.

By default these resolve relative to the source checkout (``src/failsafe``
-> repo root), so they work from any CWD and from an editable install. A
regular wheel install has no ``config/``, ``data/`` or ``logs/`` next to the
package, so point ``FAILSAFE_HOME`` at a directory that holds them.

``FAILSAFE_RESULTS_DIR`` moves only the generated results files
(``RESULTS*.md``, ``REPORT.html``, ``POLICY_DASHBOARD.html``, ...). It
defaults to the project root (where the committed copies live); the Docker
image sets it to ``/app/logs/results`` so a non-root user can write them.
"""

import os
from pathlib import Path

# src/failsafe/paths.py -> repo root is three levels up.
_CHECKOUT_ROOT = Path(__file__).resolve().parent.parent.parent

PROJECT_ROOT = (
    Path(os.environ["FAILSAFE_HOME"]).resolve()
    if os.environ.get("FAILSAFE_HOME")
    else _CHECKOUT_ROOT
)

CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"

RESULTS_DIR = (
    Path(os.environ["FAILSAFE_RESULTS_DIR"]).resolve()
    if os.environ.get("FAILSAFE_RESULTS_DIR")
    else PROJECT_ROOT
)
