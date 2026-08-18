from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tweeter_data_fetcher.paths import PROJECT_ROOT
from tweeter_data_fetcher.account_config import (
    DEFAULT_PRIORITY_POLICIES,
    DEFAULT_TIER_CONFIGURATION,
    TierConfig,
    get_priority_policy,
    load_tier_config as _load_tier_config,
    ordered_accounts,
)


def resolve_config_path(
    explicit: Optional[str | Path] = None,
    *,
    project_root: Path = PROJECT_ROOT,
    filename: str = "config.json",
) -> Path:
    candidates: List[str | Path] = []
    if explicit:
        raw = Path(explicit)
        candidates.append(raw)
        if not raw.is_absolute():
            candidates.append(Path.cwd() / raw)
            candidates.append(project_root / raw)
            # CLI paths are often repo-root relative while PROJECT_ROOT is twitter_fetcher/.
            candidates.append(project_root.parent / raw)
            if raw.parts and raw.parts[0] == project_root.name:
                candidates.append(project_root.parent / raw)
                candidates.append(project_root / Path(*raw.parts[1:]))
    if filename == "config.json":
        candidates.append(os.environ.get("TDF_CONFIG"))
    candidates.append(project_root / "config" / filename)
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists():
            return path.resolve()
    path = Path(explicit) if explicit else project_root / "config" / filename
    return (path if path.is_absolute() else project_root / path).resolve()


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


def load_tier_config(config: Dict) -> Tuple[Dict[str, Dict], Dict[int, Dict]]:
    if "tier_configuration" not in config:
        accounts = load_json_config(filename="accounts.json", default=DEFAULT_TIER_CONFIGURATION)
        config = {**config, "tier_configuration": accounts}
    return _load_tier_config(config)


__all__ = [
    "DEFAULT_PRIORITY_POLICIES",
    "DEFAULT_TIER_CONFIGURATION",
    "TierConfig",
    "get_priority_policy",
    "load_json_config",
    "load_tier_config",
    "ordered_accounts",
    "resolve_config_path",
]
