#!/usr/bin/env python3
"""Guard config.json against the frozen known-good capture.

The baseline under ``shared/config/known_good_contracts/`` is a byte-for-byte
copy of a sniffer run in which every endpoint returned HTTP 200 and the
captured query IDs, feature maps, and field toggles all matched config at
capture time. This script re-checks the live ``config.json`` against that
frozen truth so payload drift (the classic 400 cause) is detected before a run.

Runtime-injected values (userId, cursor, rawQuery, querySource) are ignored --
only structural / feature-flag drift is reported.

Exit code 0 = no drift, 1 = drift found (or a baseline/config file is missing).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# endpoint -> api_config query-id key
QUERY_ID_KEYS = {
    "UserTweetsAndReplies": "user_tweets_and_replies_query_id",
    "SearchTimeline": "search_timeline_query_id",
    "UserTweets": "user_tweets_query_id",
}
# keys the runner injects at request time; never compared for value/presence
RUNTIME_KEYS = {"userId", "cursor", "rawQuery", "querySource"}

_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = _ROOT / "src" / "shared" / "config" / "config.json"
BASELINE_DIR = _ROOT / "src" / "shared" / "config" / "known_good_contracts" / "endpoint_contracts"


def _load(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _diff_features(config_feats: Dict[str, Any], base_feats: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    for key in sorted(set(base_feats) - set(config_feats)):
        problems.append(f"feature missing in config: {key}")
    for key in sorted(set(config_feats) - set(base_feats)):
        problems.append(f"feature extra in config (not in capture): {key}")
    for key in sorted(set(config_feats) & set(base_feats)):
        if config_feats[key] != base_feats[key]:
            problems.append(
                f"feature value differs: {key} config={config_feats[key]} capture={base_feats[key]}"
            )
    return problems


def _diff_toggles(config_tg: Any, base_tg: Any) -> List[str]:
    # SearchTimeline: both absent/null is a match, not drift.
    config_tg = config_tg or {}
    base_tg = base_tg or {}
    problems: List[str] = []
    for key in sorted(set(base_tg) - set(config_tg)):
        problems.append(f"fieldToggle missing in config: {key}")
    for key in sorted(set(config_tg) - set(base_tg)):
        problems.append(f"fieldToggle extra in config: {key}")
    for key in sorted(set(config_tg) & set(base_tg)):
        if config_tg[key] != base_tg[key]:
            problems.append(
                f"fieldToggle value differs: {key} config={config_tg[key]} capture={base_tg[key]}"
            )
    return problems


def _diff_variable_keys(config_vars: Dict[str, Any], base_vars: Dict[str, Any]) -> List[str]:
    config_keys = set(config_vars) - RUNTIME_KEYS
    base_keys = set(base_vars) - RUNTIME_KEYS
    problems: List[str] = []
    for key in sorted(base_keys - config_keys):
        problems.append(f"variable key missing in config: {key}")
    for key in sorted(config_keys - base_keys):
        problems.append(f"variable key extra in config: {key}")
    return problems


def check_endpoint(endpoint: str, config: Dict[str, Any]) -> List[str]:
    base_path = BASELINE_DIR / f"{endpoint}.json"
    if not base_path.exists():
        return [f"baseline contract not found: {base_path}"]
    captured = _load(base_path).get("captured", {})
    payload = config.get("graphql_endpoint_payloads", {}).get(endpoint, {})
    if not payload:
        return [f"config has no graphql_endpoint_payloads entry for {endpoint}"]

    problems: List[str] = []
    # query id
    cfg_qid = config.get("api_config", {}).get(QUERY_ID_KEYS[endpoint], "")
    if cfg_qid != captured.get("query_id"):
        problems.append(
            f"query_id differs: config={cfg_qid!r} capture={captured.get('query_id')!r}"
        )
    problems += _diff_features(payload.get("features", {}), captured.get("features", {}))
    problems += _diff_toggles(payload.get("fieldToggles"), captured.get("fieldToggles"))
    problems += _diff_variable_keys(
        payload.get("variables", {}).get("initial", {}),
        captured.get("variables_sample", {}),
    )
    return problems


def main() -> int:
    if not CONFIG_PATH.exists():
        print(f"[ERROR] config not found: {CONFIG_PATH}")
        return 1
    config = _load(CONFIG_PATH)
    any_drift = False
    for endpoint in QUERY_ID_KEYS:
        problems = check_endpoint(endpoint, config)
        if problems:
            any_drift = True
            print(f"[DRIFT] {endpoint}: {len(problems)} difference(s) vs frozen capture")
            for line in problems:
                print(f"    - {line}")
        else:
            print(f"[OK] {endpoint}: matches baseline")
    print("---")
    print("DRIFT DETECTED — reconcile config.json with the capture." if any_drift
          else "No drift. config.json matches the frozen known-good capture.")
    return 1 if any_drift else 0


if __name__ == "__main__":
    sys.exit(main())
