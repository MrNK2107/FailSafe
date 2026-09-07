"""
Single source of truth for every on-disk location the pipeline touches.

Every module used to compute its own paths via
``Path(__file__).parent.parent / "data" / ...`` - duplicated ~40 times and
broken the moment the layout changed. Import from here instead; these
constants resolve relative to the package location, so they work from any
CWD, editable installs and wheels included.
"""

from pathlib import Path

# src/failsafe/paths.py -> repo root is three levels up.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
