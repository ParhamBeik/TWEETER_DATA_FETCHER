"""Paths, config-file resolution, and account tier policy.

The engine always runs as a subprocess launched by ``fetching.runner`` with
``TDF_PROJECT_ROOT`` (an ephemeral scratch dir) and ``TDF_CONFIG`` set, so every
path here resolves from that root. Postgres is the durable store; the scratch
tree is deleted after each run.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Scratch root written by the runner. Falls back to cwd, which is also what the
# runner sets as the subprocess working directory.
PROJECT_ROOT = Path(os.environ.get("TDF_PROJECT_ROOT") or Path.cwd()).resolve()

CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"

# Historical and live intentionally SHARE one data tree; search is isolated.
HISTORICAL_LIVE_SUBSYSTEM = "historical_live"
SEARCH_SUBSYSTEM = "search"
HISTORICAL_LIVE_DIR = DATA_DIR / HISTORICAL_LIVE_SUBSYSTEM
SEARCH_DIR = DATA_DIR / SEARCH_SUBSYSTEM


def resolve_config_path(
    explicit: Optional[str | Path] = None,
    *,
    project_root: Path = PROJECT_ROOT,
    filename: str = "config.json",
) -> Path:
    """First existing of: explicit path, $TDF_CONFIG, <root>/config/<filename>."""
    candidates: List[Any] = []
    if explicit:
        raw = Path(explicit)
        candidates += [raw, Path.cwd() / raw, project_root / raw]
    if filename == "config.json":
        candidates.append(os.environ.get("TDF_CONFIG"))
    default = project_root / "config" / filename
    candidates.append(default)
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate).resolve()
    return default.resolve()


def load_json_config(
    explicit: Optional[str | Path] = None,
    *,
    project_root: Path = PROJECT_ROOT,
    filename: str = "config.json",
    default: Any = None,
) -> Any:
    path = resolve_config_path(explicit, project_root=project_root, filename=filename)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


# --- Account tiers ---------------------------------------------------------
#
# Priority 1 is polled fastest and keeps the widest rolling window; 7 is the
# fallback for any account with no configured tier.
DEFAULT_PRIORITY_POLICIES: Dict[int, Dict] = {
    1: {"poll_interval_seconds": 120, "live_window_hours": 24, "historical_window_days": 7},
    2: {"poll_interval_seconds": 240, "live_window_hours": 20, "historical_window_days": 6},
    3: {"poll_interval_seconds": 360, "live_window_hours": 16, "historical_window_days": 5},
    4: {"poll_interval_seconds": 540, "live_window_hours": 12, "historical_window_days": 4},
    5: {"poll_interval_seconds": 780, "live_window_hours": 9, "historical_window_days": 3},
    6: {"poll_interval_seconds": 1020, "live_window_hours": 6, "historical_window_days": 2},
    7: {"poll_interval_seconds": 1440, "live_window_hours": 3, "historical_window_days": 2},
}

FALLBACK_PRIORITY = 7


def _priority_from_key(key: str) -> int:
    try:
        return int(key.split("_", 1)[1]) if key.startswith("priority_") else FALLBACK_PRIORITY
    except (ValueError, IndexError):
        return FALLBACK_PRIORITY


def load_tier_config(config: Dict) -> Tuple[Dict[str, Dict], Dict[int, Dict]]:
    """Build {handle: metadata} plus {priority: policy}.

    ``tier_configuration`` is written into the scratch config by the runner from
    the Postgres account table; ``accounts.json`` is the fallback for a scratch
    dir built without it.
    """
    tier_cfg = config.get("tier_configuration")
    if tier_cfg is None:
        tier_cfg = load_json_config(filename="accounts.json", default={}) or {}
    policy_cfg = config.get("priority_policies", {})

    policy_map: Dict[int, Dict] = {}
    for priority, defaults in DEFAULT_PRIORITY_POLICIES.items():
        override = policy_cfg.get(str(priority), {}) or {}
        policy_map[priority] = {
            "priority": priority,
            **{key: int(override.get(key, value)) for key, value in defaults.items()},
        }

    account_map: Dict[str, Dict] = {}
    for key, records in tier_cfg.items():
        priority = _priority_from_key(key)
        if priority not in policy_map:
            priority = FALLBACK_PRIORITY
        for record in records or []:
            username = str(record.get("username", "")).strip()
            if not username:
                continue
            account_map[username.lower()] = {
                "username": username,
                "display_name": str(record.get("display_name") or username).strip() or username,
                "priority": priority,
            }
    return account_map, policy_map


def get_priority_policy(
    username: str, account_map: Dict[str, Dict], policy_map: Dict[int, Dict]
) -> Dict:
    """Return the policy for ``username``, falling back to priority 7."""
    meta = account_map.get(username.lower()) or {}
    priority = meta.get("priority", FALLBACK_PRIORITY)
    return {
        **policy_map.get(priority, policy_map[FALLBACK_PRIORITY]),
        "username": meta.get("username", username),
        "display_name": meta.get("display_name", username),
        "priority": priority,
    }


def ordered_accounts(account_map: Dict[str, Dict]) -> List[str]:
    """Usernames by priority, preserving configured order within each tier."""
    rows = sorted(account_map.values(), key=lambda row: int(row.get("priority", FALLBACK_PRIORITY)))
    return [row["username"] for row in rows if row.get("username")]


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (OSError, ValueError):
        return default


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
