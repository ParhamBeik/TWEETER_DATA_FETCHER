from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from tweeter_data_fetcher.paths import CONFIG_DIR


QUERY_ID_KEYS = {
    "UserTweetsAndReplies": "user_tweets_and_replies_query_id",
    "SearchTimeline": "search_timeline_query_id",
    "UserTweets": "user_tweets_query_id",
}
RUNTIME_KEYS = {"userId", "cursor", "rawQuery", "querySource"}
BASELINE_DIR = CONFIG_DIR / "known_good_contracts" / "endpoint_contracts"


def _load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _mapping_diff(prefix: str, current: Dict[str, Any], baseline: Dict[str, Any]) -> List[str]:
    problems = [f"{prefix} missing in config: {key}" for key in sorted(set(baseline) - set(current))]
    problems += [f"{prefix} extra in config: {key}" for key in sorted(set(current) - set(baseline))]
    problems += [
        f"{prefix} value differs: {key} config={current[key]} capture={baseline[key]}"
        for key in sorted(set(current) & set(baseline))
        if current[key] != baseline[key]
    ]
    return problems


def check_endpoint(
    endpoint: str,
    config: Dict[str, Any],
    *,
    baseline_dir: Path = BASELINE_DIR,
) -> List[str]:
    base_path = baseline_dir / f"{endpoint}.json"
    if not base_path.exists():
        return [f"baseline contract not found: {base_path}"]
    captured = _load(base_path).get("captured", {})
    payload = config.get("graphql_endpoint_payloads", {}).get(endpoint, {})
    if not payload:
        return [f"config has no graphql_endpoint_payloads entry for {endpoint}"]
    problems: List[str] = []
    query_id = config.get("api_config", {}).get(QUERY_ID_KEYS[endpoint], "")
    if query_id != captured.get("query_id"):
        problems.append(f"query_id differs: config={query_id!r} capture={captured.get('query_id')!r}")
    problems += _mapping_diff("feature", payload.get("features", {}), captured.get("features", {}))
    problems += _mapping_diff(
        "fieldToggle",
        payload.get("fieldToggles") or {},
        captured.get("fieldToggles") or {},
    )
    current_keys = set(payload.get("variables", {}).get("initial", {})) - RUNTIME_KEYS
    baseline_keys = set(captured.get("variables_sample", {})) - RUNTIME_KEYS
    problems += [f"variable key missing in config: {key}" for key in sorted(baseline_keys - current_keys)]
    problems += [f"variable key extra in config: {key}" for key in sorted(current_keys - baseline_keys)]
    return problems


def verify_contract(
    config_path: Path,
    *,
    baseline_dir: Path = BASELINE_DIR,
) -> Dict[str, List[str]]:
    if not config_path.exists():
        return {"config": [f"config not found: {config_path}"]}
    if not baseline_dir.exists():
        return {}
    config = _load(config_path)
    return {
        endpoint: problems
        for endpoint in QUERY_ID_KEYS
        if (problems := check_endpoint(endpoint, config, baseline_dir=baseline_dir))
    }
