"""Central path routing for the tweeter_data_fetcher package.

Every directory path is defined here. All modules import from this module
instead of re-deriving project paths from ``__file__`` depth arithmetic, so
future restructuring only requires editing this one file.

All paths resolve relative to ``twitter_fetcher/`` (the parent folder that
holds ``src/``, ``tests/``, ``diagnostics/``, ``config/`` and ``data/``),
independent of the current working directory.
"""

from pathlib import Path


# twitter_fetcher/src/tweeter_data_fetcher/paths.py -> parents[2] == twitter_fetcher/
PROJECT_ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"

# Subsystem names. Historical and live intentionally SHARE one data tree.
HISTORICAL_LIVE_SUBSYSTEM = "historical_live"
SEARCH_SUBSYSTEM = "search"

HISTORICAL_LIVE_DIR = DATA_DIR / HISTORICAL_LIVE_SUBSYSTEM
SEARCH_DIR = DATA_DIR / SEARCH_SUBSYSTEM

# Diagnostic experiment scripts and their report artifacts. Kept separate from
# the pytest suite under tests/.
DIAGNOSTICS_DIR = PROJECT_ROOT / "diagnostics"
DIAGNOSTIC_REPORTS_DIR = DIAGNOSTICS_DIR / "reports"
