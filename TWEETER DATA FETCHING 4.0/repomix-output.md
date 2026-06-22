This file is a merged representation of the entire codebase, combined into a single document by Repomix.

# File Summary

## Purpose
This file contains a packed representation of the entire repository's contents.
It is designed to be easily consumable by AI systems for analysis, code review,
or other automated processes.

## File Format
The content is organized as follows:
1. This summary section
2. Repository information
3. Directory structure
4. Repository files (if enabled)
5. Multiple file entries, each consisting of:
  a. A header with the file path (## File: path/to/file)
  b. The full contents of the file in a code block

## Usage Guidelines
- This file should be treated as read-only. Any changes should be made to the
  original repository files, not this packed version.
- When processing this file, use the file path to distinguish
  between different files in the repository.
- Be aware that this file may contain sensitive information. Handle it with
  the same level of security as you would the original repository.

## Notes
- Some files may have been excluded based on .gitignore rules and Repomix's configuration
- Binary files are not included in this packed representation. Please refer to the Repository Structure section for a complete list of file paths, including binary files
- Files matching patterns in .gitignore are excluded
- Files matching default ignore patterns are excluded
- Files are sorted by Git change count (files with more changes are at the bottom)

# Directory Structure
```
historical_scripts/
  historical_runner.py
live_scripts/
  live_runner.py
  live_storage.py
  viral_detector.py
search_scripts/
  search_runner.py
shared/
  auth/
    __init__.py
    session_updater.py
    setup_api_cookies.py
  config/
    __init__.py
    config.json
    search_config.json
    tier_config.py
  core/
    __init__.py
    api_manager.py
    fetcher_engine.py
    set_operations.py
    windowing.py
  data_pipeline/
    __init__.py
    storage_manager.py
  exporters/
    __init__.py
    text_export_helper.py
  tools/
    check_replies_parity.py
    diagnose_replies_only.py
.gitignore
structure.txt
```

# Files

## File: historical_scripts/historical_runner.py
```python
#!/usr/bin/env python3
"""Canonical v4 replies-first fetch and processing pipeline."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.config.tier_config import get_priority_policy, ordered_accounts
from shared.core.fetcher_engine import FetcherEngine
from shared.core.set_operations import TweetSetProcessor
from shared.core.windowing import RollingWindowEvaluator
from shared.data_pipeline.storage_manager import StorageManager


ENDPOINTS = ("UserTweetsAndReplies", "UserTweets")


def _endpoint_pages(storage: StorageManager, username: str, endpoint: str) -> List[Dict[str, Any]]:
    state = storage.get_endpoint_state(username, endpoint)
    raw_batch_path = state.get("raw_batch_path")
    if not raw_batch_path:
        return storage.load_all_raw_pages(endpoint, username, include_legacy=True)
    return storage.load_raw_pages_from_batch(raw_batch_path)


def _endpoint_raw_batch_path(storage: StorageManager, username: str, endpoint: str) -> Optional[Path]:
    state = storage.get_endpoint_state(username, endpoint)
    raw_batch_path = state.get("raw_batch_path")
    if not raw_batch_path:
        return None
    path = Path(str(raw_batch_path))
    return path if path.exists() and path.is_dir() else None


def _historical_window_days_for(engine: FetcherEngine, username: str) -> int:
    policy = get_priority_policy(username, engine.account_map, engine.priority_policies)
    return int(policy.get("historical_window_days", 1))


def _endpoint_window_coverage(
    *,
    evaluator: RollingWindowEvaluator,
    storage: StorageManager,
    username: str,
    endpoint: str,
    window_days: int,
) -> Tuple[bool, Dict[str, Any], List[Dict[str, Any]]]:
    raw_pages = storage.load_all_raw_pages(endpoint, username, include_legacy=True)
    coverage = evaluator.evaluate_raw_pages(raw_pages, username, endpoint, window_days)
    return coverage.complete, coverage.__dict__, raw_pages


def _verify_raw_pages(
    *,
    storage: StorageManager,
    username: str,
    endpoint: str,
    raw_pages: List[Dict[str, Any]],
) -> bool:
    batch_path = _endpoint_raw_batch_path(storage, username, endpoint)
    page_files = sorted(batch_path.glob("page_*.json")) if batch_path else []
    ok = bool(raw_pages) and bool(page_files) and len(page_files) >= len(raw_pages)
    if ok:
        print(f"[V4] @{username} {endpoint} raw verified: {len(page_files)} page file(s)")
    else:
        print(
            f"[V4] @{username} {endpoint} raw verification warning: "
            f"loaded_pages={len(raw_pages)} page_files={len(page_files)}"
        )
    return ok


def _verify_txt_files(username: str, label: str, paths: List[Path]) -> bool:
    missing_or_empty = [path for path in paths if not path.exists() or path.stat().st_size == 0]
    if not paths or missing_or_empty:
        print(
            f"[V4] @{username} {label} TXT verification warning: "
            f"files={len(paths)} missing_or_empty={len(missing_or_empty)}"
        )
        return False
    print(f"[V4] @{username} {label} TXT verified: {len(paths)} file(s)")
    return True


def _process_account(
    *,
    storage: StorageManager,
    processor: TweetSetProcessor,
    username: str,
    raw_pages_by_endpoint: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Tuple[bool, Dict[str, int]]:
    raw_pages_by_endpoint = raw_pages_by_endpoint or {}
    raw_replies = raw_pages_by_endpoint.get("UserTweetsAndReplies") or _endpoint_pages(
        storage, username, "UserTweetsAndReplies"
    )
    raw_tweets = raw_pages_by_endpoint.get("UserTweets") or _endpoint_pages(
        storage, username, "UserTweets"
    )

    set_a = processor.extract_tweets_from_raw(
        raw_tweets,
        username=username,
        source_endpoint="UserTweets",
    )
    set_b = processor.extract_tweets_from_raw(
        raw_replies,
        username=username,
        source_endpoint="UserTweetsAndReplies",
    )

    list_a = list(set_a.values())
    list_b = list(set_b.values())
    list_intersection = processor.get_intersection(set_a, set_b)
    list_union = processor.get_union(set_a, set_b)
    list_replies_only = processor.get_difference_b_minus_a(set_a, set_b)

    txt_outputs = {
        "1_user_tweets": storage.save_processed_txt_set(list_a, "1_user_tweets", username),
        "2_user_tweets_and_replies": storage.save_processed_txt_set(list_b, "2_user_tweets_and_replies", username),
        "3_intersection": storage.save_processed_txt_set(list_intersection, "3_intersection", username),
        "4_union": storage.save_processed_txt_set(list_union, "4_union", username),
        "5_replies_only": storage.save_processed_txt_set(list_replies_only, "5_replies_only", username),
    }
    verified = all(_verify_txt_files(username, set_name, paths) for set_name, paths in txt_outputs.items())

    print(
        f"[V4] @{username} processed | "
        f"tweets={len(list_a)} replies_endpoint={len(list_b)} "
        f"intersection={len(list_intersection)} union={len(list_union)} "
        f"replies_only={len(list_replies_only)}"
    )
    return verified, {
        "tweets": len(list_a),
        "replies_endpoint": len(list_b),
        "intersection": len(list_intersection),
        "union": len(list_union),
        "replies_only": len(list_replies_only),
    }


def _save_endpoint_processed_txt(
    *,
    storage: StorageManager,
    processor: TweetSetProcessor,
    username: str,
    endpoint: str,
    raw_pages: List[Dict[str, Any]],
) -> bool:
    if not raw_pages:
        raw_pages = _endpoint_pages(storage, username, endpoint)
    set_name = "2_user_tweets_and_replies" if endpoint == "UserTweetsAndReplies" else "1_user_tweets"
    extracted = processor.extract_tweets_from_raw(
        raw_pages,
        username=username,
        source_endpoint=endpoint,
    )
    paths = storage.save_processed_txt_set(list(extracted.values()), set_name, username)
    print(f"[V4] @{username} {endpoint} TXT updated: {len(extracted)} item(s)")
    raw_ok = _verify_raw_pages(
        storage=storage,
        username=username,
        endpoint=endpoint,
        raw_pages=raw_pages,
    )
    txt_ok = _verify_txt_files(username, set_name, paths)
    return raw_ok and txt_ok


def _safe_endpoint_report(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": result.get("status"),
        "outcome": result.get("outcome"),
        "reason": result.get("reason"),
        "pages_fetched": result.get("pages_fetched", 0),
        "raw_batch_path": result.get("raw_batch_path"),
        "last_cursor": result.get("last_cursor"),
        "last_http_status": result.get("last_http_status"),
        "attempts": result.get("attempts", 0),
        "error_samples": result.get("error_samples", []),
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "window_coverage": result.get("window_coverage"),
    }


def _update_report_summary(report: Dict[str, Any]) -> None:
    summary = {
        "successful_endpoints": 0,
        "partial_endpoints": 0,
        "failed_endpoints": 0,
        "skipped_endpoints": 0,
        "txt_unverified_endpoints": 0,
    }
    for account_report in report.get("accounts", {}).values():
        for endpoint_report in (account_report.get("endpoints", {}) or {}).values():
            status = endpoint_report.get("status")
            if status == "completed":
                summary["successful_endpoints"] += 1
            elif status == "partial":
                summary["partial_endpoints"] += 1
            elif status == "failed":
                summary["failed_endpoints"] += 1
            elif status == "skipped":
                summary["skipped_endpoints"] += 1
            if endpoint_report.get("processed_txt_verified") is False:
                summary["txt_unverified_endpoints"] += 1
    report["summary"] = summary


def _print_report_summary(report: Dict[str, Any], json_path: Path, txt_path: Path) -> None:
    _update_report_summary(report)
    print("[V4] Run summary:")
    for key, value in report.get("summary", {}).items():
        print(f"[V4]   {key}: {value}")

    grouped: Dict[str, List[str]] = {"failed": [], "partial": [], "skipped": []}
    for username, account_report in report.get("accounts", {}).items():
        for endpoint, endpoint_report in (account_report.get("endpoints", {}) or {}).items():
            status = endpoint_report.get("status")
            if status in grouped:
                grouped[status].append(f"@{username} {endpoint} ({endpoint_report.get('outcome')})")
    for status, items in grouped.items():
        if items:
            print(f"[V4]   {status}: {', '.join(items)}")
    print(f"[V4] Report JSON: {json_path}")
    print(f"[V4] Report TXT: {txt_path}")


def run_v4(selected_accounts: Optional[List[str]] = None) -> None:
    project_root = Path(__file__).resolve().parent.parent
    engine = FetcherEngine(config_path="shared/config/config.json")
    storage = StorageManager(project_root=project_root, subsystem="historical_live")
    processor = TweetSetProcessor()
    evaluator = RollingWindowEvaluator()
    migration_report = storage.migrate_legacy_historical_data(verify=True)
    print(f"[V4] Historical storage migration: {migration_report}")

    accounts = ordered_accounts(engine.account_map) if selected_accounts is None else selected_accounts
    if not accounts:
        print("[V4] No accounts found in tier configuration.")
        return

    accounts = [account.strip().lstrip("@") for account in accounts if account and account.strip()]
    run_id = storage.create_run_id()
    report: Dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.utcnow().isoformat() + "Z",
        "config": {
            "endpoint_order": ["UserTweetsAndReplies", "UserTweets"],
            "accounts_requested": len(accounts),
            "completion_rule": "tehran_jalali_rolling_window",
            "pagination_safety_cap_pages": engine.pagination_safety_cap_pages,
            "first_request_warmup_seconds": engine.first_request_warmup_seconds,
            "historical_storage_migration": migration_report,
        },
        "summary": {},
        "accounts": {},
    }
    user_ids: Dict[str, str] = {}
    active_accounts: List[str] = []
    fetched_pages: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        username: {} for username in accounts
    }

    print(f"[V4] Phase 1/4: resolving user ids for {len(accounts)} account(s)")
    for username in accounts:
        storage.ensure_account_state(username)
        report["accounts"].setdefault(username, {"endpoints": {}})
        try:
            user_ids[username] = engine._get_user_id(username)
        except Exception as exc:
            reason = f"UserByScreenName failed: {exc}"
            storage.mark_account_skipped_for_run(username, reason)
            report["accounts"][username]["skip_reason"] = reason
            for endpoint in ENDPOINTS:
                report["accounts"][username]["endpoints"][endpoint] = {
                    "status": "skipped",
                    "outcome": "skipped_user_id",
                    "reason": reason,
                    "pages_fetched": 0,
                    "processed_txt_verified": None,
                }
            print(f"[V4] @{username} skipped for this run: {reason}")
            continue
        active_accounts.append(username)
        report["accounts"][username]["user_id"] = user_ids[username]
        print(f"[V4] @{username} -> user_id={user_ids[username]}")

    if not active_accounts:
        print("[V4] No accounts with resolved user ids; nothing to fetch.")
        report["finished_at"] = datetime.utcnow().isoformat() + "Z"
        _update_report_summary(report)
        json_path = storage.save_run_report_json(report, run_id)
        txt_path = storage.save_run_report_txt(report, run_id)
        _print_report_summary(report, json_path, txt_path)
        return

    print("[V4] Phase 2/4: fetching UserTweetsAndReplies for all accounts")
    for idx, username in enumerate(active_accounts):
        window_days = _historical_window_days_for(engine, username)
        window_complete, window_coverage, existing_pages = _endpoint_window_coverage(
            evaluator=evaluator,
            storage=storage,
            username=username,
            endpoint="UserTweetsAndReplies",
            window_days=window_days,
        )
        if window_complete:
            result = {
                "account": username,
                "endpoint": "UserTweetsAndReplies",
                "status": "completed",
                "outcome": "skipped_window_complete",
                "reason": "Existing data satisfies rolling window",
                "pages": existing_pages,
                "pages_fetched": len(existing_pages),
                "raw_batch_path": str(_endpoint_raw_batch_path(storage, username, "UserTweetsAndReplies") or ""),
                "last_cursor": "__WINDOW_COMPLETE__",
                "last_http_status": None,
                "attempts": 0,
                "error_samples": [],
                "started_at": datetime.utcnow().isoformat() + "Z",
                "finished_at": datetime.utcnow().isoformat() + "Z",
                "window_coverage": window_coverage,
            }
        else:
            result = engine._fetch_endpoint_result(
                account=username,
                user_id=user_ids[username],
                endpoint="UserTweetsAndReplies",
                max_pages=engine.pagination_safety_cap_pages,
                window_days=window_days,
                force_refetch=True,
            )
        fetched_pages[username]["UserTweetsAndReplies"] = result.get("pages", [])
        endpoint_verified = _save_endpoint_processed_txt(
            storage=storage,
            processor=processor,
            username=username,
            endpoint="UserTweetsAndReplies",
            raw_pages=fetched_pages[username]["UserTweetsAndReplies"],
        )
        endpoint_report = _safe_endpoint_report(result)
        endpoint_report["processed_txt_verified"] = endpoint_verified
        report["accounts"][username]["endpoints"]["UserTweetsAndReplies"] = endpoint_report
        storage.update_endpoint_state(
            username,
            "UserTweetsAndReplies",
            meta={
                "processed_txt_verified": endpoint_verified,
                "run_id": run_id,
                "outcome": result.get("outcome"),
                "pages_fetched": result.get("pages_fetched", 0),
                "window_coverage": result.get("window_coverage") or window_coverage,
            },
        )
        if idx < len(active_accounts) - 1:
            engine.api_manager.human_delay("between_accounts")

    print("[V4] Phase 3/4: fetching UserTweets for all accounts")
    for idx, username in enumerate(active_accounts):
        window_days = _historical_window_days_for(engine, username)
        window_complete, window_coverage, existing_pages = _endpoint_window_coverage(
            evaluator=evaluator,
            storage=storage,
            username=username,
            endpoint="UserTweets",
            window_days=window_days,
        )
        if window_complete:
            result = {
                "account": username,
                "endpoint": "UserTweets",
                "status": "completed",
                "outcome": "skipped_window_complete",
                "reason": "Existing data satisfies rolling window",
                "pages": existing_pages,
                "pages_fetched": len(existing_pages),
                "raw_batch_path": str(_endpoint_raw_batch_path(storage, username, "UserTweets") or ""),
                "last_cursor": "__WINDOW_COMPLETE__",
                "last_http_status": None,
                "attempts": 0,
                "error_samples": [],
                "started_at": datetime.utcnow().isoformat() + "Z",
                "finished_at": datetime.utcnow().isoformat() + "Z",
                "window_coverage": window_coverage,
            }
        else:
            result = engine._fetch_endpoint_result(
                account=username,
                user_id=user_ids[username],
                endpoint="UserTweets",
                max_pages=engine.pagination_safety_cap_pages,
                window_days=window_days,
                force_refetch=True,
            )
        fetched_pages[username]["UserTweets"] = result.get("pages", [])
        endpoint_verified = _save_endpoint_processed_txt(
            storage=storage,
            processor=processor,
            username=username,
            endpoint="UserTweets",
            raw_pages=fetched_pages[username]["UserTweets"],
        )
        endpoint_report = _safe_endpoint_report(result)
        endpoint_report["processed_txt_verified"] = endpoint_verified
        report["accounts"][username]["endpoints"]["UserTweets"] = endpoint_report
        storage.update_endpoint_state(
            username,
            "UserTweets",
            meta={
                "processed_txt_verified": endpoint_verified,
                "run_id": run_id,
                "outcome": result.get("outcome"),
                "pages_fetched": result.get("pages_fetched", 0),
                "window_coverage": result.get("window_coverage") or window_coverage,
            },
        )
        if idx < len(active_accounts) - 1:
            engine.api_manager.human_delay("between_accounts")

    print("[V4] Phase 4/4: generating processed TXT sets")
    for username in active_accounts:
        final_verified, counts = _process_account(
            storage=storage,
            processor=processor,
            username=username,
            raw_pages_by_endpoint=fetched_pages.get(username),
        )
        report["accounts"][username]["final_sets"] = {
            "verified": final_verified,
            "counts": counts,
        }
        storage.update_account_state(
            username,
            lambda state, verified=final_verified, count_data=counts, current_run_id=run_id: state.update({
                "processed_sets_verified": verified,
                "processed_counts": count_data,
                "last_run_id": current_run_id,
            }),
        )

    report["finished_at"] = datetime.utcnow().isoformat() + "Z"
    _update_report_summary(report)
    json_path = storage.save_run_report_json(report, run_id)
    txt_path = storage.save_run_report_txt(report, run_id)
    _print_report_summary(report, json_path, txt_path)
    print("[V4] Replies-first pipeline complete.")


if __name__ == "__main__":
    run_v4()
```

## File: live_scripts/live_runner.py
```python
#!/usr/bin/env python3
"""
V4 isolated live monitoring subsystem.

This script intentionally does not route through main_orchestrator.py.
User-facing operation is continuous; run with --once only for validation.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.config.tier_config import get_priority_policy, load_tier_config, ordered_accounts
from shared.core.fetcher_engine import FetcherEngine
from shared.core.set_operations import TweetSetProcessor
from shared.core.windowing import parse_twitter_timestamp
from live_scripts.live_storage import LiveStorageManager
from live_scripts.viral_detector import ViralDetector


class LiveMonitor:
    """Poll UserTweets and UserTweetsAndReplies shallowly per account."""

    ENDPOINTS = ("UserTweets", "UserTweetsAndReplies")

    def __init__(self, config_path: str = "shared/config/config.json"):
        self.project_root = Path(__file__).resolve().parents[1]
        self.fetcher = FetcherEngine(config_path=config_path, subsystem="live")
        self.api_manager = self.fetcher.api_manager
        self.config = self.api_manager.config
        self.account_map, self.priority_policies = load_tier_config(self.config)
        self.accounts = ordered_accounts(self.account_map)
        self.processor = TweetSetProcessor()
        self.live_storage = LiveStorageManager(self.project_root)
        self.viral_detector = ViralDetector(config_path=config_path, storage=self.live_storage)
        viral_cfg = self.config.get("viral_detection", {})
        self.snapshot_min_delta = int(viral_cfg.get("snapshot_min_metric_delta", 25))
        self.snapshot_min_minutes = int(viral_cfg.get("snapshot_min_minutes", 10))

    @staticmethod
    def _tweet_dt(tweet: Dict[str, Any]) -> Optional[datetime]:
        parsed = parse_twitter_timestamp(tweet.get("raw_timestamp") or tweet.get("created_at"))
        return parsed.replace(tzinfo=None) if parsed else None

    def _within_live_window(self, tweet: Dict[str, Any], hours: int) -> bool:
        tweet_dt = self._tweet_dt(tweet)
        if not tweet_dt:
            return True
        return tweet_dt >= (datetime.utcnow() - timedelta(hours=max(1, int(hours))))

    @staticmethod
    def _compact_json(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    def _get_live_user_id(self, username: str) -> str:
        state = self.live_storage.account_state(username)
        cached_user_id = state.get("user_id")
        if cached_user_id:
            return str(cached_user_id)

        endpoint = "UserByScreenName"
        query_id = self.api_manager.get_query_id(endpoint)
        if not query_id:
            raise RuntimeError("Missing query ID for UserByScreenName")
        variables = {"screen_name": username, "withSafetyModeUserFields": True}
        features = {
            "hidden_profile_subscriptions_enabled": True,
            "rweb_tipjar_consumption_enabled": True,
        }
        request_url = self.fetcher._build_graphql_url(
            endpoint=endpoint,
            query_id=query_id,
            variables=variables,
            features=features,
        )
        response = self.api_manager.perform_get(endpoint=endpoint, url=request_url, username=username)
        response.raise_for_status()
        payload = response.json()
        user_id = (
            payload.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("rest_id")
        )
        if not user_id:
            raise RuntimeError(f"Could not resolve user id for @{username}")
        self.live_storage.update_account_state(username, {"user_id": str(user_id)})
        return str(user_id)

    def should_fetch_account(self, username: str) -> bool:
        policy = get_priority_policy(username, self.account_map, self.priority_policies)
        interval = int(policy.get("poll_interval_seconds", 300))
        state = self.live_storage.account_state(username)
        last = state.get("last_checked_at")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", ""))
        except Exception:
            return True
        return (datetime.utcnow() - last_dt).total_seconds() >= interval

    def _fetch_live_endpoint(self, username: str, user_id: str, endpoint: str, live_window_hours: int, safety_cap_pages: int) -> Dict[str, Any]:
        started_at = datetime.utcnow().isoformat() + "Z"
        pages: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        attempts = 0
        last_http_status: Optional[int] = None
        cursor: Optional[str] = None
        status = "completed"
        outcome = "success_window_complete"
        reason = "Live window crossed"

        def record_http_error(response, cursor_value: Optional[str], attempt_number: int) -> None:
            nonlocal last_http_status
            last_http_status = int(response.status_code)
            errors.append({
                "status_code": int(response.status_code),
                "cursor": cursor_value,
                "attempt": attempt_number,
                "response_text": str(response.text or "")[:500],
            })

        def classify_http_failure(status_code: int, has_pages: bool, cursor_value: Optional[str]) -> tuple[str, str, str]:
            if status_code == 404 and cursor_value and has_pages:
                return "partial", "partial_cursor_404", "Cursor returned 404 after successful pages"
            if status_code == 404:
                return "failed", "failed_initial_404", "Initial page returned 404"
            if status_code in {401, 403}:
                if has_pages:
                    return "partial", "partial_http_error", f"HTTP {status_code} after successful pages"
                return "failed", "failed_initial_auth", f"Initial request returned HTTP {status_code}"
            if status_code == 429:
                if has_pages:
                    return "partial", "partial_rate_limited", "Rate limit persisted after successful pages"
                return "failed", "failed_initial_rate_limit", "Initial request stayed rate-limited"
            if 500 <= status_code < 600:
                if has_pages:
                    return "partial", "partial_http_error", f"HTTP {status_code} after successful pages"
                return "failed", "failed_initial_http_error", f"Initial request returned HTTP {status_code}"
            if has_pages:
                return "partial", "partial_http_error", f"HTTP {status_code} after successful pages"
            return "failed", "failed_initial_http_error", f"Initial request returned HTTP {status_code}"

        try:
            query_id = self.api_manager.get_query_id(endpoint)
            if not query_id:
                raise RuntimeError(f"Missing query ID for {endpoint}")
            features = self.fetcher._timeline_features(endpoint)
            field_toggles = self.fetcher._timeline_field_toggles(endpoint)
            retry_policy = self.api_manager.retry_policy()
            max_attempts = max(
                int(retry_policy.get("client_error_attempts", self.fetcher.max_cursor_error_retries)),
                int(retry_policy.get("server_error_attempts", self.fetcher.max_cursor_error_retries)),
                int(retry_policy.get("request_error_attempts", self.fetcher.max_cursor_error_retries)),
            )
            self.api_manager.warmup_navigation_context(username=username, endpoint=endpoint)
            if self.fetcher.first_request_warmup_seconds > 0:
                time.sleep(self.fetcher.first_request_warmup_seconds)

            for page_number in range(1, max(1, int(safety_cap_pages)) + 1):
                variables = self.fetcher._timeline_variables(endpoint, user_id, cursor)
                request_url = self.fetcher._build_graphql_url(
                    endpoint=endpoint,
                    query_id=query_id,
                    variables=variables,
                    features=features,
                    field_toggles=field_toggles,
                )
                request_headers = {
                    "referer": f"https://x.com/{username}/with_replies" if endpoint == "UserTweetsAndReplies" else f"https://x.com/{username}",
                    "x-twitter-active-user": "yes",
                }
                response = None
                page_request_succeeded = False
                for attempt in range(max_attempts):
                    attempts += 1
                    try:
                        response = self.api_manager.perform_get(
                            endpoint=endpoint,
                            url=request_url,
                            max_retries=1,
                            username=username,
                            headers=request_headers,
                        )
                        last_http_status = int(response.status_code)
                        if response.status_code == 429:
                            record_http_error(response, cursor, attempt + 1)
                            wait = self.api_manager.rate_limit_sleep_seconds(endpoint, response.headers)
                            if wait <= 0:
                                wait = int(retry_policy.get("rate_limit_safety_buffer_seconds", 5))
                            if attempt >= max_attempts - 1:
                                status, outcome, reason = classify_http_failure(429, bool(pages), cursor)
                                break
                            time.sleep(wait)
                            continue
                        if response.status_code in {400, 401, 403, 404}:
                            record_http_error(response, cursor, attempt + 1)
                            client_attempts = int(retry_policy.get("client_error_attempts", self.fetcher.max_cursor_error_retries))
                            if attempt < client_attempts - 1:
                                self.api_manager.jitter_sleep(
                                    float(retry_policy.get("client_error_min_seconds", 10)),
                                    float(retry_policy.get("client_error_max_seconds", 20)),
                                    reason=f"@{username} {endpoint} HTTP {response.status_code} retry {attempt + 1}/{client_attempts}",
                                )
                                continue
                            status, outcome, reason = classify_http_failure(int(response.status_code), bool(pages), cursor)
                            break
                        if 500 <= response.status_code < 600:
                            record_http_error(response, cursor, attempt + 1)
                            server_attempts = int(retry_policy.get("server_error_attempts", self.fetcher.max_cursor_error_retries))
                            if attempt < server_attempts - 1:
                                base = float(retry_policy.get("server_error_base_seconds", 5))
                                max_sleep = float(retry_policy.get("server_error_max_seconds", 60))
                                wait = min(max_sleep, base * (2 ** attempt))
                                self.api_manager.jitter_sleep(wait, wait + base, reason=f"@{username} {endpoint} HTTP {response.status_code}")
                                continue
                            status, outcome, reason = classify_http_failure(int(response.status_code), bool(pages), cursor)
                            break
                        response.raise_for_status()
                        page_request_succeeded = True
                        break
                    except Exception as exc:
                        errors.append({"cursor": cursor, "attempt": attempt + 1, "exception": str(exc)[:500]})
                        request_attempts = int(retry_policy.get("request_error_attempts", self.fetcher.max_cursor_error_retries))
                        if attempt < request_attempts - 1:
                            base = float(retry_policy.get("request_error_base_seconds", 5))
                            max_sleep = float(retry_policy.get("request_error_max_seconds", 60))
                            wait = min(max_sleep, base * (2 ** attempt))
                            self.api_manager.jitter_sleep(wait, wait + base, reason=f"@{username} {endpoint} request error")
                            continue
                        status = "partial" if pages else "failed"
                        outcome = "partial_request_error" if pages else "failed_initial_request_error"
                        reason = str(exc)[:500]
                        break
                if not page_request_succeeded:
                    break
                payload = response.json()
                pages.append(payload)
                self.live_storage.save_raw_page(username, endpoint, page_number, payload)
                page_tweets = list(
                    self.processor.extract_tweets_from_raw([payload], username=username, source_endpoint=endpoint).values()
                )
                if page_tweets and all(not self._within_live_window(tweet, live_window_hours) for tweet in page_tweets):
                    outcome = "success_live_window_crossed"
                    reason = "Page contained only tweets older than live window"
                    break
                cursor = self.fetcher._extract_bottom_cursor(payload)
                if not cursor:
                    outcome = "success_true_end"
                    reason = "End of pagination reached without cursor"
                    break
            else:
                status = "partial"
                outcome = "partial_safety_cap_reached"
                reason = "Emergency safety page cap reached before live window crossed"
        except Exception as exc:
            return {
                "account": username,
                "endpoint": endpoint,
                "status": "failed",
                "outcome": "live_exception",
                "reason": str(exc)[:500],
                "pages": [],
                "pages_fetched": 0,
                "last_http_status": last_http_status,
                "attempts": attempts,
                "finished_at": datetime.utcnow().isoformat() + "Z",
            }
        return {
            "account": username,
            "endpoint": endpoint,
            "status": status,
            "outcome": outcome,
            "reason": reason,
            "pages": pages,
            "pages_fetched": len(pages),
            "last_cursor": cursor or "__END__",
            "last_http_status": last_http_status,
            "attempts": attempts,
            "error_samples": errors[-5:],
            "started_at": started_at,
            "finished_at": datetime.utcnow().isoformat() + "Z",
        }

    def _process_sets(self, username: str, endpoint_pages: Dict[str, List[Dict[str, Any]]], live_window_hours: int) -> Dict[str, List[Dict[str, Any]]]:
        set_a = self.processor.extract_tweets_from_raw(endpoint_pages.get("UserTweets", []), username=username, source_endpoint="UserTweets")
        set_b = self.processor.extract_tweets_from_raw(endpoint_pages.get("UserTweetsAndReplies", []), username=username, source_endpoint="UserTweetsAndReplies")
        outputs = {
            "1_user_tweets": list(set_a.values()),
            "2_user_tweets_and_replies": list(set_b.values()),
            "3_intersection": self.processor.get_intersection(set_a, set_b),
            "4_union": self.processor.get_union(set_a, set_b),
            "5_replies_only": self.processor.get_difference_b_minus_a(set_a, set_b),
        }
        for key, tweets in outputs.items():
            filtered = [tweet for tweet in tweets if self._within_live_window(tweet, live_window_hours)]
            outputs[key] = filtered
            self.live_storage.save_processed_set(username, key, filtered)
        return outputs

    def _handle_new_tweets(self, username: str, tweets: List[Dict[str, Any]]) -> Dict[str, int]:
        new_count = 0
        duplicate_count = 0
        viral_reports = 0
        for tweet in tweets:
            tweet_id = str(tweet.get("id") or tweet.get("rest_id") or "").strip()
            if not tweet_id:
                continue
            was_seen = self.live_storage.is_seen(tweet_id)
            if was_seen:
                duplicate_count += 1
            else:
                new_count += 1
            self.live_storage.register_tweet(tweet, [f"live/{username}"])
            self.live_storage.save_snapshot(
                tweet,
                force=not was_seen,
                min_delta=self.snapshot_min_delta,
                min_minutes=self.snapshot_min_minutes,
            )
            analysis = self.viral_detector.analyze_tweet(tweet_id, str(tweet.get("account") or username), tweet)
            if analysis:
                self.live_storage.save_viral_report(analysis)
                viral_reports += 1
        return {"new": new_count, "duplicates": duplicate_count, "viral_reports": viral_reports}

    def monitor_account(self, username: str) -> Dict[str, Any]:
        policy = get_priority_policy(username, self.account_map, self.priority_policies)
        live_window_hours = int(policy.get("live_window_hours", 24))
        result: Dict[str, Any] = {
            "account": username,
            "priority": policy.get("priority"),
            "started_at": datetime.utcnow().isoformat() + "Z",
            "endpoints": {},
        }
        try:
            user_id = self._get_live_user_id(username)
        except Exception as exc:
            result["status"] = "failed"
            result["reason"] = f"user_id_resolution_failed: {str(exc)[:300]}"
            self.live_storage.update_account_state(username, {"last_checked_at": datetime.utcnow().isoformat() + "Z", "last_status": "failed"})
            return result

        endpoint_pages: Dict[str, List[Dict[str, Any]]] = {}
        for endpoint in self.ENDPOINTS:
            endpoint_result = self._fetch_live_endpoint(
                username,
                user_id,
                endpoint,
                live_window_hours=live_window_hours,
                safety_cap_pages=self.fetcher.pagination_safety_cap_pages,
            )
            result["endpoints"][endpoint] = {k: v for k, v in endpoint_result.items() if k != "pages"}
            endpoint_pages[endpoint] = endpoint_result.get("pages", [])

        sets = self._process_sets(username, endpoint_pages, live_window_hours)
        new_stats = self._handle_new_tweets(username, sets["4_union"])
        result["sets"] = {key: len(value) for key, value in sets.items()}
        result["new_tweets"] = new_stats
        result["status"] = "completed"
        result["finished_at"] = datetime.utcnow().isoformat() + "Z"
        self.live_storage.update_account_state(
            username,
            {
                "last_checked_at": result["finished_at"],
                "last_status": result["status"],
                "last_counts": result["sets"],
            },
        )
        return result

    def run_cycle(self, only_accounts: Optional[List[str]] = None) -> Dict[str, Any]:
        selected = only_accounts or self.accounts
        report = {
            "started_at": datetime.utcnow().isoformat() + "Z",
            "accounts": {},
            "summary": {"checked": 0, "skipped": 0, "failed": 0},
        }
        for username in selected:
            if not self.should_fetch_account(username):
                report["summary"]["skipped"] += 1
                continue
            account_report = self.monitor_account(username)
            report["accounts"][username] = account_report
            report["summary"]["checked"] += 1
            if account_report.get("status") != "completed":
                report["summary"]["failed"] += 1
            self.api_manager.human_delay("between_accounts")
        report["finished_at"] = datetime.utcnow().isoformat() + "Z"
        return report

    def run_continuous(self, only_accounts: Optional[List[str]] = None, check_interval: int = 60) -> None:
        print("Starting v4 live monitor. Press Ctrl+C to stop.")
        while True:
            report = self.run_cycle(only_accounts=only_accounts)
            print(f"Live cycle complete: {report['summary']}")
            sim = self.config.get("anti_bot_simulation", {})
            if sim.get("enabled", True):
                delays = sim.get("delays_seconds", {})
                extra = self.api_manager.jitter_sleep(
                    float(delays.get("between_cycles_min", 0)),
                    float(delays.get("between_cycles_max", 60)),
                    reason="live cycle pacing",
                )
                sleep_for = max(0, check_interval - int(extra))
            else:
                sleep_for = check_interval
            time.sleep(max(1, sleep_for))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run isolated v4 live monitoring.")
    parser.add_argument("--config", default="shared/config/config.json")
    parser.add_argument("--account", action="append", dest="accounts", help="Limit to one account; can be repeated.")
    parser.add_argument("--once", action="store_true", help="Run one internal validation cycle instead of continuous mode.")
    parser.add_argument("--check-interval", type=int, default=60)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    monitor = LiveMonitor(config_path=args.config)
    if args.once:
        print(monitor.run_cycle(only_accounts=args.accounts))
    else:
        monitor.run_continuous(only_accounts=args.accounts, check_interval=args.check_interval)


if __name__ == "__main__":
    main()
```

## File: live_scripts/live_storage.py
```python
#!/usr/bin/env python3
"""
Isolated v4 live-monitoring storage and viral-report helpers.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shared.data_pipeline.storage_manager import StorageManager, extract_metrics


class LiveStorageManager:
    """Keep live state and outputs separate from historical sync state."""

    def __init__(self, project_root: Optional[Path] = None, timezone: str = "Asia/Tehran"):
        # تغییر parent به parents[2] برای رسیدن به ریشه پروژه
        self.project_root = project_root or Path(__file__).resolve().parents[1]
        self.storage = StorageManager(base_dir=self.project_root, timezone=timezone, subsystem="historical_live")
        self.data_root = self.project_root / "data" / "historical_live"
        self.raw_root = self.data_root / "raw"
        self.processed_root = self.data_root / "processed"
        self.reports_root = self.data_root / "reports"
        self.viral_root = self.data_root / "viral"
        self.snapshots_root = self.viral_root / "snapshots"
        self.state_dir = self.data_root / "state"
        self.live_state_file = self.state_dir / "live_state.json"
        self.seen_tweets_file = self.state_dir / "seen_tweets.json"
        self.snapshot_index_file = self.state_dir / "snapshot_index.json"
        self._ensure_dirs()
        self.live_state = self._load_json(self.live_state_file, {})
        self.seen_tweets = self._load_json(self.seen_tweets_file, {})
        self.snapshot_index = self._load_json(self.snapshot_index_file, {})

    def _ensure_dirs(self) -> None:
        for path in [self.raw_root, self.processed_root, self.reports_root, self.viral_root, self.snapshots_root, self.state_dir]:
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, type(default)) else default
            except Exception:
                return default
        return default

    @staticmethod
    def _save_json(path: Path, payload: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path

    @staticmethod
    def safe_slug(value: str, max_len: int = 80) -> str:
        slug = re.sub(r"[^A-Za-z0-9_\\-]+", "_", str(value or "unknown").strip())
        return (slug.strip("_") or "unknown")[:max_len]

    def now(self) -> datetime:
        return self.storage._tehran_now()

    def batch_name(self) -> str:
        return self.storage._jalali_batch_name(self.now())

    def raw_batch_dir(self, username: str, endpoint: str) -> Path:
        target = self.raw_root / endpoint / self.safe_slug(username.lower()) / self.batch_name()
        target.mkdir(parents=True, exist_ok=True)
        return target

    def save_raw_page(self, username: str, endpoint: str, page_number: int, payload: Dict[str, Any]) -> Path:
        return self.storage.save_raw_page(self.raw_batch_dir(username, endpoint), page_number, payload)

    def account_state(self, username: str) -> Dict[str, Any]:
        key = username.lower().lstrip("@")
        state = self.live_state.get(key, {})
        return state if isinstance(state, dict) else {}

    def update_account_state(self, username: str, updates: Dict[str, Any]) -> Path:
        key = username.lower().lstrip("@")
        current = self.account_state(username)
        current.update(updates)
        self.live_state[key] = current
        return self._save_json(self.live_state_file, self.live_state)

    def is_seen(self, tweet_id: str) -> bool:
        return str(tweet_id) in self.seen_tweets

    def register_tweet(self, tweet: Dict[str, Any], stored_in: List[str]) -> None:
        tweet_id = str(tweet.get("id") or tweet.get("rest_id") or "").strip()
        if not tweet_id:
            return
        existing = self.seen_tweets.get(tweet_id, {})
        locations = set(existing.get("stored_in", [])) if isinstance(existing, dict) else set()
        locations.update(stored_in)
        self.seen_tweets[tweet_id] = {
            "tweet_id": tweet_id,
            "account": tweet.get("account"),
            "first_seen_at": existing.get("first_seen_at") if isinstance(existing, dict) else datetime.utcnow().isoformat() + "Z",
            "last_seen_at": datetime.utcnow().isoformat() + "Z",
            "stored_in": sorted(locations),
        }
        self._save_json(self.seen_tweets_file, self.seen_tweets)

    def save_processed_set(self, username: str, set_name: str, tweets: List[Dict[str, Any]]) -> Dict[str, Path]:
        folder = self.storage.SET_FOLDER_MAP.get(set_name, self.storage.SET_FOLDER_MAP.get(str(set_name).upper(), set_name))
        target = self.processed_root / folder / self.safe_slug(username.lower())
        target.mkdir(parents=True, exist_ok=True)
        output_json = target / f"{folder}.json"
        self._save_json(output_json, tweets or [])
        output_txt = target / f"{folder}.txt"
        self.storage.save_processed_txt(tweets or [], output_txt)
        return {"json": output_json, "txt": output_txt}

    def should_save_snapshot(self, tweet_id: str, metrics: Dict[str, Any], min_delta: int, min_minutes: int) -> Tuple[bool, str]:
        snapshots = self.load_snapshots(tweet_id)
        if not snapshots:
            return True, "first_snapshot"
        latest = snapshots[-1]
        try:
            last_ts = datetime.fromisoformat(str(latest.get("timestamp")))
            minutes = (datetime.utcnow() - last_ts.replace(tzinfo=None)).total_seconds() / 60.0
        except Exception:
            minutes = float(min_minutes)
        if minutes >= min_minutes:
            return True, "time_threshold"
        for key in ("likes", "retweets", "replies", "quotes", "bookmarks", "views"):
            try:
                if abs(int(metrics.get(key, 0) or 0) - int(latest.get(key, 0) or 0)) >= min_delta:
                    return True, f"{key}_delta"
            except Exception:
                continue
        return False, "below_snapshot_threshold"

    def save_snapshot(self, tweet: Dict[str, Any], force: bool = False, min_delta: int = 25, min_minutes: int = 10) -> Optional[Path]:
        tweet_id = str(tweet.get("id") or tweet.get("rest_id") or "").strip()
        if not tweet_id:
            return None
        metrics = extract_metrics({"legacy": {
            "favorite_count": tweet.get("likes", 0),
            "retweet_count": tweet.get("retweets", 0),
            "reply_count": tweet.get("replies", 0),
            "quote_count": tweet.get("quotes", 0),
            "bookmark_count": tweet.get("bookmarks", 0),
        }, "views": {"count": tweet.get("views", 0)}})
        should_save, reason = self.should_save_snapshot(tweet_id, metrics, min_delta, min_minutes)
        if not force and not should_save:
            return None

        account = self.safe_slug(str(tweet.get("account") or "unknown").lower())
        target = self.snapshots_root / account
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{account}_{tweet_id}.json"
        snapshots = self.load_snapshots(tweet_id)
        snapshots.append({
            "timestamp": datetime.utcnow().isoformat(),
            "reason": reason,
            "tweet_id": tweet_id,
            "account": tweet.get("account"),
            **metrics,
        })
        self._save_json(path, snapshots)
        self.snapshot_index[tweet_id] = str(path.relative_to(self.project_root / "data"))
        self._save_json(self.snapshot_index_file, self.snapshot_index)
        return path

    def load_snapshots(self, tweet_id: str) -> List[Dict[str, Any]]:
        rel = self.snapshot_index.get(str(tweet_id))
        candidates = []
        if rel:
            candidates.append(self.project_root / "data" / rel)
        candidates.extend(self.snapshots_root.glob(f"*/*_{tweet_id}.json"))
        for path in candidates:
            if path.exists():
                data = self._load_json(path, [])
                return sorted(data if isinstance(data, list) else [], key=lambda row: str(row.get("timestamp", "")))
        return []

    def save_viral_report(self, analysis: Dict[str, Any]) -> Dict[str, Path]:
        tweet_id = self.safe_slug(str(analysis.get("tweet_id", "unknown")))
        label = "confirmed" if analysis.get("confirmed") else "candidate"
        timestamp = self.storage._jalali_batch_name(self.now())
        base = self.viral_root / "reports" / label
        base.mkdir(parents=True, exist_ok=True)
        json_path = base / f"{timestamp}_{tweet_id}.json"
        txt_path = base / f"{timestamp}_{tweet_id}.txt"
        self._save_json(json_path, analysis)
        lines = [
            f"Viral {label}: {analysis.get('classification', 'UNKNOWN')}",
            f"Tweet ID: {analysis.get('tweet_id')}",
            f"Account: @{analysis.get('account')}",
            f"Score: {analysis.get('score')}",
            f"Confirmed: {analysis.get('confirmed')}",
            "",
            str((analysis.get("tweet") or {}).get("text") or ""),
        ]
        txt_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return {"json": json_path, "txt": txt_path}
```

## File: live_scripts/viral_detector.py
```python
#!/usr/bin/env python3
"""
V4 live viral detection using isolated live snapshots.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from live_scripts.live_storage import LiveStorageManager


class ViralDetector:
    """Detect viral candidates from engagement velocity and acceleration."""

    def __init__(self, config_path: str = "shared/config/config.json", storage: Optional[LiveStorageManager] = None):
        self.project_root = PROJECT_ROOT
        cfg_path = Path(config_path)
        if not cfg_path.is_absolute():
            cfg_path = self.project_root / cfg_path
        self.config = self._load_config(cfg_path)
        self.viral_config = self.config.get("viral_detection", self.config.get("viral_config", {}))
        self.storage = storage or LiveStorageManager(self.project_root)
        self.threshold_percentile = int(self.viral_config.get("threshold_percentile", 95))
        self.composite_cutoff = float(self.viral_config.get("composite_score_cutoff", 1.0))
        self.account_baselines: Dict[str, Dict[str, float]] = {}

    @staticmethod
    def _load_config(path: Path) -> Dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _num(value: Any) -> float:
        if value in (None, "unknown", "UNKNOWN"):
            return 0.0
        try:
            return float(str(value).replace(",", ""))
        except Exception:
            return 0.0

    def load_snapshots(self, tweet_id: str) -> List[Dict[str, Any]]:
        return self.storage.load_snapshots(tweet_id)

    def calculate_velocity(self, snapshots: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if len(snapshots) < 2:
            return None
        try:
            first_time = datetime.fromisoformat(str(snapshots[0]["timestamp"]))
            last_time = datetime.fromisoformat(str(snapshots[-1]["timestamp"]))
            minutes = (last_time - first_time).total_seconds() / 60.0
            if minutes <= 0:
                return None
        except Exception:
            return None
        velocity = {"time_window_minutes": minutes, "snapshot_count": len(snapshots)}
        for metric in ["likes", "retweets", "replies", "views", "bookmarks", "quotes"]:
            velocity[f"{metric}_per_min"] = (self._num(snapshots[-1].get(metric)) - self._num(snapshots[0].get(metric))) / minutes
        return velocity

    def calculate_multi_window_velocity(self, snapshots: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        parsed = []
        for snap in snapshots:
            try:
                parsed.append((datetime.fromisoformat(str(snap["timestamp"])), snap))
            except Exception:
                continue
        if len(parsed) < 2:
            return None
        parsed.sort(key=lambda row: row[0])
        latest_ts, latest = parsed[-1]
        result: Dict[str, Any] = {}
        for window in [5, 30, 120]:
            start = None
            for ts, snap in reversed(parsed):
                if (latest_ts - ts).total_seconds() / 60.0 >= window:
                    start = snap
                    break
            if not start:
                continue
            for metric in ["likes", "retweets", "replies", "views", "quotes", "bookmarks"]:
                result[f"{metric}_per_min_{window}m"] = (self._num(latest.get(metric)) - self._num(start.get(metric))) / float(window)
        return result or None

    def calculate_acceleration(self, snapshots: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if len(snapshots) < 3:
            return None
        mid = len(snapshots) // 2
        first = self.calculate_velocity(snapshots[: mid + 1])
        second = self.calculate_velocity(snapshots[mid:])
        if not first or not second:
            return None
        return {
            f"{metric}_acceleration": second.get(f"{metric}_per_min", 0) - first.get(f"{metric}_per_min", 0)
            for metric in ["likes", "retweets", "replies", "views", "bookmarks", "quotes"]
        }

    def calculate_engagement_quality(self, metrics: Dict[str, Any]) -> float:
        views = max(self._num(metrics.get("views")), 1.0)
        engagement = sum(self._num(metrics.get(key)) for key in ["likes", "retweets", "replies", "quotes"])
        return engagement / views

    def calculate_momentum(self, snapshots: List[Dict[str, Any]]) -> float:
        if len(snapshots) < 4:
            return 0.0
        likes = [self._num(snap.get("likes")) for snap in snapshots[-5:]]
        diffs = [likes[idx] - likes[idx - 1] for idx in range(1, len(likes))]
        return (diffs[-1] - diffs[0]) if len(diffs) >= 2 else 0.0

    def get_account_baseline(self, account: str) -> Dict[str, float]:
        key = str(account or "unknown").lower()
        if key in self.account_baselines:
            return self.account_baselines[key]
        values: Dict[str, List[float]] = defaultdict(list)
        for folder in ["1_user_tweets", "2_user_tweets_and_replies", "4_union"]:
            path = self.project_root / "data" / "historical_live" / "processed" / folder / key / f"{folder}.json"
            if not path.exists():
                continue
            try:
                tweets = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                tweets = []
            for tweet in tweets if isinstance(tweets, list) else []:
                for metric in ["likes", "retweets", "views"]:
                    values[metric].append(self._num(tweet.get(metric)))
        baseline: Dict[str, float] = {}
        for metric, metric_values in values.items():
            ordered = sorted(v for v in metric_values if v > 0)
            if ordered:
                idx = min(len(ordered) - 1, int(len(ordered) * self.threshold_percentile / 100.0))
                baseline[f"{metric}_p{self.threshold_percentile}"] = ordered[idx]
        self.account_baselines[key] = baseline
        return baseline

    def classify_viral(
        self,
        tweet_id: str,
        account: str,
        current_metrics: Dict[str, Any],
        velocity: Dict[str, Any],
        acceleration: Optional[Dict[str, Any]] = None,
        snapshots: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[bool, str, float]:
        baseline = self.get_account_baseline(account)
        if not baseline:
            likes = self._num(current_metrics.get("likes"))
            views = self._num(current_metrics.get("views"))
            if likes > 10000 and views > 1000000:
                return True, "HIGH_ABSOLUTE_ENGAGEMENT", 2.0
            if likes > 5000 and views > 500000:
                return True, "MODERATE_ABSOLUTE_ENGAGEMENT", 1.5
            return False, "NORMAL", 0.5

        multi = self.calculate_multi_window_velocity(snapshots or [])
        if multi:
            velocity = dict(velocity)
            for metric in ["likes", "views", "retweets"]:
                velocity[f"{metric}_per_min"] = (
                    multi.get(f"{metric}_per_min_5m", 0) * 0.5
                    + multi.get(f"{metric}_per_min_30m", 0) * 0.3
                    + multi.get(f"{metric}_per_min_120m", 0) * 0.2
                )

        score = 0.0
        for metric, weight in [("likes", 0.4), ("views", 0.3), ("retweets", 0.3)]:
            baseline_value = baseline.get(f"{metric}_p{self.threshold_percentile}", 1)
            if baseline_value > 0:
                score += (self._num(velocity.get(f"{metric}_per_min")) / (baseline_value / 1440.0)) * weight
        quality = self.calculate_engagement_quality(current_metrics)
        score += 0.8 if quality > 0.08 else (0.4 if quality > 0.05 else (-0.5 if quality < 0.01 else 0))
        momentum = self.calculate_momentum(snapshots or [])
        score += 1.0 if momentum > 50 else (0.5 if momentum > 10 else (-0.5 if momentum < -10 else 0))
        spread = self._num(current_metrics.get("retweets")) / max(self._num(current_metrics.get("likes")), 1.0)
        score += 0.6 if spread > 0.25 else (0.3 if spread > 0.15 else 0)
        if acceleration and self._num(acceleration.get("likes_acceleration")) > 0:
            score += 0.5

        if score >= 4.0:
            return True, "BREAKOUT_TRAJECTORY", score
        if score >= 2.0:
            return True, "STRONG_GROWTH", score
        if score >= self.composite_cutoff:
            return True, "VIRAL_CANDIDATE", score
        return False, "NORMAL", score

    def analyze_tweet(self, tweet_id: str, account: str, tweet_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        snapshots = self.load_snapshots(tweet_id)
        if len(snapshots) < 2:
            return None
        velocity = self.calculate_velocity(snapshots)
        if not velocity:
            return None
        acceleration = self.calculate_acceleration(snapshots)
        current_metrics = snapshots[-1]
        is_viral, classification, score = self.classify_viral(tweet_id, account, current_metrics, velocity, acceleration, snapshots)
        if not is_viral:
            return None
        return {
            "tweet_id": tweet_id,
            "account": account,
            "tweet": tweet_data,
            "metrics": current_metrics,
            "velocity": velocity,
            "acceleration": acceleration,
            "classification": classification,
            "score": score,
            "confirmed": score >= 2.0,
            "analyzed_at": datetime.utcnow().isoformat() + "Z",
        }
```

## File: search_scripts/search_runner.py
```python
#!/usr/bin/env python3
"""
V4 isolated Advanced SearchTimeline monitor.

Raw pages are stored as data/search/raw/{search_slug}/{product}/{jalali_batch}/page_{i}.json.
This script intentionally does not route through main_orchestrator.py.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote, urlencode

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.config.tier_config import DEFAULT_PRIORITY_POLICIES
from shared.core.fetcher_engine import FetcherEngine
from shared.core.set_operations import TweetSetProcessor
from shared.core.windowing import parse_twitter_timestamp
from shared.data_pipeline.storage_manager import StorageManager


VALID_PRODUCTS = {"Top", "Latest", "Media", "People"}

FROZEN_SEARCH_FEATURES: Dict[str, object] = {
    "articles_preview_enabled": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_inline_media_enabled": False,
    "longform_notetweets_rich_text_read_enabled": True,
    "post_ctas_fetch_enabled": True,
    "premium_content_api_read_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_grok_analysis_button_from_backend": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_profile_redirect_enabled": False,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "rweb_cashtags_composer_attachment_enabled": True,
    "rweb_cashtags_enabled": True,
    "rweb_conversational_replies_downvote_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "rweb_video_screen_enabled": False,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "verified_phone_label_enabled": False,
    "view_counts_everywhere_api_enabled": True,
}


class SearchQueryBuilder:
    """Build SearchTimeline rawQuery and browser URL from search definitions."""

    @staticmethod
    def _sanitize_term(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    @staticmethod
    def _ensure_handle(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "", str(value or "").strip().lstrip("@"))

    @staticmethod
    def _quote_phrase(value: str) -> str:
        text = SearchQueryBuilder._sanitize_term(value).replace('"', "")
        return f"\"{text}\"" if text else ""

    @staticmethod
    def normalize_product(value: str) -> str:
        candidate = str(value or "Top").strip().title()
        return candidate if candidate in VALID_PRODUCTS else "Top"

    @staticmethod
    def build_raw_query(search_def: Dict[str, Any], now_dt: datetime) -> str:
        if search_def.get("raw_query"):
            return str(search_def["raw_query"]).strip()
        if bool(search_def.get("preserve_exact_query", False)):
            explicit = str(search_def.get("exact_query") or "").strip()
            if explicit:
                return explicit

        parts: List[str] = []
        include_keywords = [SearchQueryBuilder._sanitize_term(term) for term in search_def.get("include_keywords", []) if SearchQueryBuilder._sanitize_term(term)]
        if include_keywords:
            parts.append(include_keywords[0] if len(include_keywords) == 1 else "(" + " OR ".join(include_keywords) + ")")
        for phrase in search_def.get("exact_phrases", []):
            clean = SearchQueryBuilder._sanitize_term(phrase).replace('"', "")
            if clean:
                parts.append(clean)
        for keyword in search_def.get("exclude_keywords", []):
            clean = SearchQueryBuilder._sanitize_term(keyword)
            if clean:
                parts.append(f"-{SearchQueryBuilder._quote_phrase(clean)}" if " " in clean else f"-{clean}")
        for key, prefix in [("from_accounts", "from:"), ("to_accounts", "to:")]:
            for account in search_def.get(key, []):
                handle = SearchQueryBuilder._ensure_handle(account)
                if handle:
                    parts.append(f"{prefix}{handle}")
        for mention in search_def.get("mentions", []):
            handle = SearchQueryBuilder._ensure_handle(mention)
            if handle:
                parts.append(f"@{handle}")
        for numeric_key in ["min_replies", "min_faves", "min_retweets"]:
            value = search_def.get(numeric_key)
            if value is not None and str(value).strip():
                parts.append(f"{numeric_key}:{int(value)}")
        lang = SearchQueryBuilder._sanitize_term(str(search_def.get("lang", "")))
        if lang:
            parts.append(f"lang:{lang}")

        since = SearchQueryBuilder._sanitize_term(str(search_def.get("since", "")))
        until = SearchQueryBuilder._sanitize_term(str(search_def.get("until", "")))
        since_days = search_def.get("since_days")
        if not since and since_days is not None:
            since = (now_dt - timedelta(days=int(since_days))).date().isoformat()
        if not until and since_days is not None and not bool(search_def.get("preserve_exact_query", False)):
            until = now_dt.date().isoformat()
        if since:
            parts.append(f"since:{since}")
        if until:
            parts.append(f"until:{until}")
        for extra in search_def.get("extra_filters", []):
            clean = SearchQueryBuilder._sanitize_term(extra)
            if clean:
                parts.append(clean)
        return " ".join(parts).strip()

    @staticmethod
    def build_human_search_url(raw_query: str, product: str) -> str:
        encoded_query = quote(raw_query, safe="()")
        filter_map = {"Top": "top", "Latest": "live", "Media": "media", "People": "user"}
        normalized = SearchQueryBuilder.normalize_product(product)
        return f"https://x.com/search?q={encoded_query}&f={filter_map.get(normalized, 'top')}&src=typed_query"

    @staticmethod
    def slug(search_def: Dict[str, Any]) -> str:
        raw = str(search_def.get("slug") or search_def.get("name") or "search_timeline")
        return re.sub(r"[^A-Za-z0-9_\\-]+", "_", raw).strip("_").lower() or "search_timeline"


class SearchTimelineMonitor:
    """Continuous SearchTimeline monitor using v4 auth/rate-limit infrastructure."""

    def __init__(self, config_path: str = "shared/config/config.json", search_config_path: str = "shared/config/search_config.json"):
        self.project_root = Path(__file__).resolve().parents[1]
        self.fetcher = FetcherEngine(config_path=config_path, subsystem="search")
        self.api_manager = self.fetcher.api_manager
        self.config = self.api_manager.config
        self.storage = StorageManager(base_dir=self.project_root, subsystem="search")
        self.processor = TweetSetProcessor()
        self.search_defs = self._load_search_config(search_config_path)
        self.search_root = self.project_root / "data" / "search"
        self.raw_root = self.search_root / "raw"
        self.processed_root = self.search_root / "processed"
        self.debug_root = self.search_root / "debug"
        self.reports_root = self.search_root / "reports"
        self.state_file = self.search_root / "state" / "search_state.json"
        self.search_state = self._load_json(self.state_file, {})
        for path in [self.raw_root, self.processed_root, self.debug_root, self.reports_root, self.state_file.parent]:
            path.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, path: str) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.project_root / candidate

    def _load_search_config(self, path: str) -> List[Dict[str, Any]]:
        cfg_path = self._resolve_path(path)
        with cfg_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("searches", [])
        return [row for row in data if isinstance(row, dict)]

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, type(default)) else default
            except Exception:
                return default
        return default

    @staticmethod
    def _save_json(path: Path, payload: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path

    def _policy_for_search(self, search_def: Dict[str, Any]) -> Dict[str, Any]:
        priority = int(search_def.get("polling_priority", 3))
        defaults = DEFAULT_PRIORITY_POLICIES.get(priority, DEFAULT_PRIORITY_POLICIES[7])
        return {
            "poll_interval_seconds": int(search_def.get("poll_interval_seconds", defaults["poll_interval_seconds"])),
            "pagination_safety_cap_pages": int(
                search_def.get(
                    "pagination_safety_cap_pages",
                    self.config.get("api_config", {}).get("pagination_safety_cap_pages", 50),
                )
            ),
            "max_retries": int(search_def.get("max_retries", 3)),
            "rolling_hours": int(search_def.get("rolling_hours", 24)),
        }

    @staticmethod
    def _compact_json(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    def _build_base_variables(self, search_def: Dict[str, Any], raw_query: str, product: str) -> Dict[str, Any]:
        count = max(1, min(int(search_def.get("count", 20)), 20))
        return {
            "rawQuery": raw_query,
            "count": count,
            "querySource": str(search_def.get("query_source", "typed_query")),
            "product": product,
            "withGrokTranslatedBio": bool(search_def.get("with_grok_translated_bio", True)),
            "withQuickPromoteEligibilityTweetFields": bool(search_def.get("with_quick_promote_eligibility_tweet_fields", False)),
        }

    def _build_frozen_headers(self, search_url: str) -> Dict[str, str]:
        headers = dict(self.api_manager.session.headers)
        headers["referer"] = search_url
        headers["x-twitter-active-user"] = "yes"
        return {str(key): str(value) for key, value in headers.items() if value is not None}

    def _extract_instructions(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        return (
            payload.get("data", {})
            .get("search_by_raw_query", {})
            .get("search_timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )

    def _collect_cursor_candidates(self, entry: Dict[str, Any], source_path: str) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        content = entry.get("content", {}) if isinstance(entry, dict) else {}
        if not isinstance(content, dict):
            return candidates
        entry_id = str(entry.get("entryId", ""))
        value = content.get("value")
        if value:
            cursor_type = str(content.get("cursorType", ""))
            is_bottom = cursor_type.lower() == "bottom" or entry_id.startswith("cursor-bottom-")
            candidates.append({
                "value": str(value),
                "source_path": source_path,
                "entry_id": entry_id,
                "typename": str(content.get("__typename", "")),
                "cursor_type": cursor_type,
                "is_bottom": is_bottom,
                "score": 100 if is_bottom else (70 if "cursor" in entry_id.lower() else 40),
            })
        for idx, item_entry in enumerate(content.get("items", []) if isinstance(content.get("items"), list) else []):
            nested = item_entry.get("item", {}).get("content", {}) if isinstance(item_entry, dict) else {}
            if isinstance(nested, dict) and nested.get("value"):
                is_bottom = str(nested.get("cursorType", "")).lower() == "bottom"
                candidates.append({
                    "value": str(nested["value"]),
                    "source_path": f"{source_path}.items[{idx}].item.content",
                    "entry_id": entry_id,
                    "typename": str(nested.get("__typename", "")),
                    "cursor_type": str(nested.get("cursorType", "")),
                    "is_bottom": is_bottom,
                    "score": 95 if is_bottom else 35,
                })
        return candidates

    def _parse_tweet_wrapper(self, wrapper: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        tweet_obj = wrapper.get("result") if isinstance(wrapper, dict) else None
        if isinstance(tweet_obj, dict):
            tweet_obj = self.processor._unwrap_tweet_result(tweet_obj)
            return self.processor._normalize_tweet(tweet_obj, source_endpoint="SearchTimeline")
        return None

    @staticmethod
    def _tweet_datetime(tweet: Dict[str, Any]) -> Optional[datetime]:
        parsed = parse_twitter_timestamp(tweet.get("raw_timestamp") or tweet.get("created_at"))
        return parsed.replace(tzinfo=None) if parsed else None

    def _parse_search_page(self, payload: Dict[str, Any], seen_ids: Set[str], capture_debug: bool) -> Dict[str, Any]:
        tweets: List[Dict[str, Any]] = []
        candidates: List[Dict[str, Any]] = []
        entry_type_counts: Dict[str, int] = defaultdict(int)
        skipped: List[Dict[str, Any]] = []
        processed: List[Dict[str, Any]] = []
        has_entries = False
        item_count = 0
        module_count = 0

        def add_tweet(wrapper: Dict[str, Any], entry_id: str, typename: str, source_path: str) -> None:
            parsed = self._parse_tweet_wrapper(wrapper)
            if not parsed:
                skipped.append({"entry_id": entry_id, "typename": typename, "reason": f"parse_failed:{source_path}"})
                return
            tweet_id = str(parsed.get("id") or "")
            if tweet_id and tweet_id not in seen_ids:
                seen_ids.add(tweet_id)
                tweets.append(parsed)
                processed.append({"entry_id": entry_id, "typename": typename, "tweet_id": tweet_id, "source_path": source_path, "status": "added"})
            elif tweet_id:
                processed.append({"entry_id": entry_id, "typename": typename, "tweet_id": tweet_id, "source_path": source_path, "status": "duplicate_ignored"})

        for inst in self._extract_instructions(payload):
            inst_type = str(inst.get("type", ""))
            entry_type_counts[f"instruction:{inst_type or 'unknown'}"] += 1
            entries = []
            if inst_type in {"TimelineReplaceEntry", "TimelinePinEntry"} and isinstance(inst.get("entry"), dict):
                entries = [inst["entry"]]
            elif inst_type == "TimelineAddEntries":
                entries = inst.get("entries", []) if isinstance(inst.get("entries"), list) else []
            else:
                skipped.append({"entry_id": "instruction", "typename": inst_type or "unknown", "reason": "unsupported_instruction_type"})
                continue
            has_entries = has_entries or bool(entries)
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                entry_id = str(entry.get("entryId", ""))
                content = entry.get("content", {}) if isinstance(entry.get("content"), dict) else {}
                typename = str(content.get("__typename", "unknown"))
                entry_type_counts[f"entry:{typename or 'unknown'}"] += 1
                candidates.extend(self._collect_cursor_candidates(entry, "timeline.entry"))
                item_content = content.get("itemContent", {}) if isinstance(content.get("itemContent"), dict) else {}
                if entry_id.startswith("tweet-") or typename == "TimelineTimelineItem":
                    item_count += 1
                    add_tweet(item_content.get("tweet_results", {}), entry_id, typename, "content.itemContent.tweet_results")
                    continue
                if isinstance(content.get("items"), list):
                    module_count += 1
                    for idx, module in enumerate(content["items"]):
                        module_item = module.get("item", {}) if isinstance(module, dict) else {}
                        module_content = module_item.get("itemContent", {}) if isinstance(module_item, dict) else {}
                        add_tweet(module_content.get("tweet_results", {}), f"{entry_id}#item{idx}", typename, "content.items.item.itemContent.tweet_results")
                    continue
                if "cursor" not in entry_id.lower():
                    skipped.append({"entry_id": entry_id, "typename": typename, "reason": "unsupported_entry_shape"})

        candidates = sorted(candidates, key=lambda row: int(row.get("score", 0)), reverse=True)
        next_cursor = str(candidates[0].get("value")) if candidates else None
        return {
            "tweets": tweets,
            "next_cursor": next_cursor,
            "has_entries": has_entries,
            "timeline_item_count": item_count,
            "timeline_module_count": module_count,
            "entry_type_counts": dict(entry_type_counts),
            "cursor_candidates": candidates,
            "selected_cursor_source": candidates[0].get("source_path") if candidates else None,
            "skipped_entries": skipped,
            "processed_entries": processed if capture_debug else [],
        }

    def _page_crossed_search_window(self, tweets: List[Dict[str, Any]], window_start: datetime) -> bool:
        dated = [self._tweet_datetime(tweet) for tweet in tweets]
        dated = [value for value in dated if value is not None]
        return bool(dated and min(dated) <= window_start)

    @staticmethod
    def classify_search_stall(
        *,
        cursor: Optional[str],
        next_cursor: Optional[str],
        has_entries: bool,
        new_items_count: int,
        cursor_history: Set[str],
    ) -> Optional[str]:
        """Classify SearchTimeline pagination stalls for testable skip/continue behavior."""
        if cursor and next_cursor and str(next_cursor) in cursor_history:
            return "repeated_cursor_history"
        if not next_cursor:
            return "no_bottom_cursor"
        if cursor and has_entries and new_items_count <= 0:
            return "no_new_tweets_on_cursor_page"
        return None

    @staticmethod
    def classify_search_failure(status_code: Optional[int], page: int, cursor: Optional[str]) -> str:
        """Classify request failures without raising the full monitor."""
        if status_code == 404 and cursor:
            return "mid_pagination_404"
        if page == 1:
            return "first_page_failed"
        if status_code == 429:
            return "rate_limited"
        if status_code:
            return f"http_{status_code}"
        return "request_failed"

    @staticmethod
    def _classify_http_failure(status_code: int, has_pages: bool, cursor_value: Optional[str]) -> str:
        if status_code == 404 and cursor_value and has_pages:
            return "partial_cursor_404"
        if status_code == 404:
            return "failed_initial_404"
        if status_code in {401, 403}:
            return "partial_http_error" if has_pages else "failed_initial_auth"
        if status_code == 429:
            return "partial_rate_limited" if has_pages else "failed_initial_rate_limit"
        if 500 <= status_code < 600:
            return "partial_http_error" if has_pages else "failed_initial_http_error"
        return "partial_http_error" if has_pages else "failed_initial_http_error"

    def _request_page(
        self,
        graphql_url: str,
        variables_template: Dict[str, Any],
        features_json: str,
        frozen_headers: Dict[str, str],
        cursor: Optional[str],
        retries: int,
        *,
        has_pages: bool = False,
    ) -> Dict[str, Any]:
        endpoint = "SearchTimeline"
        retry_policy = self.api_manager.retry_policy()
        max_attempts = max(
            max(1, retries),
            int(retry_policy.get("client_error_attempts", self.fetcher.max_cursor_error_retries)),
            int(retry_policy.get("server_error_attempts", self.fetcher.max_cursor_error_retries)),
            int(retry_policy.get("request_error_attempts", self.fetcher.max_cursor_error_retries)),
        )
        errors: List[Dict[str, Any]] = []
        attempts = 0
        for attempt in range(max_attempts):
            attempts += 1
            try:
                variables = dict(variables_template)
                if cursor:
                    variables["cursor"] = cursor
                params = {
                    "variables": self._compact_json(variables),
                    "features": features_json,
                }
                response = self.api_manager.session.get(
                    graphql_url,
                    params=params,
                    headers=frozen_headers,
                    timeout=self.api_manager.default_timeout,
                )
                self.api_manager.last_status_by_endpoint[endpoint] = response.status_code
                self.api_manager.update_rate_limit(endpoint, response.headers)
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except Exception as exc:
                        return {
                            "_failure": "partial_parse_error" if has_pages else "failed_initial_parse_error",
                            "_status": response.status_code,
                            "_attempts": attempts,
                            "_error_samples": [{"cursor": cursor, "attempt": attempt + 1, "exception": f"JSON parse error: {str(exc)[:500]}"}],
                        }
                    payload["_attempts"] = attempts
                    payload["_error_samples"] = errors[-5:]
                    payload["_status"] = response.status_code
                    return payload
                if response.status_code == 429:
                    errors.append({"status_code": 429, "cursor": cursor, "attempt": attempt + 1, "response_text": str(response.text or "")[:500]})
                    wait = self.api_manager.rate_limit_sleep_seconds(endpoint, response.headers)
                    if wait <= 0:
                        wait = int(retry_policy.get("rate_limit_safety_buffer_seconds", 5))
                    if attempt >= max_attempts - 1:
                        return {"_failure": self._classify_http_failure(429, has_pages, cursor), "_status": 429, "_attempts": attempts, "_error_samples": errors[-5:]}
                    time.sleep(wait)
                    continue
                if response.status_code in {400, 401, 403, 404}:
                    errors.append({"status_code": int(response.status_code), "cursor": cursor, "attempt": attempt + 1, "response_text": str(response.text or "")[:500]})
                    client_attempts = int(retry_policy.get("client_error_attempts", self.fetcher.max_cursor_error_retries))
                    if attempt < client_attempts - 1:
                        self.api_manager.jitter_sleep(
                            float(retry_policy.get("client_error_min_seconds", 10)),
                            float(retry_policy.get("client_error_max_seconds", 20)),
                            reason=f"SearchTimeline HTTP {response.status_code} retry {attempt + 1}/{client_attempts}",
                        )
                        continue
                    return {"_failure": self._classify_http_failure(int(response.status_code), has_pages, cursor), "_status": int(response.status_code), "_attempts": attempts, "_error_samples": errors[-5:]}
                if 500 <= response.status_code < 600:
                    errors.append({"status_code": int(response.status_code), "cursor": cursor, "attempt": attempt + 1, "response_text": str(response.text or "")[:500]})
                    server_attempts = int(retry_policy.get("server_error_attempts", self.fetcher.max_cursor_error_retries))
                    if attempt < server_attempts - 1:
                        base = float(retry_policy.get("server_error_base_seconds", 5))
                        max_sleep = float(retry_policy.get("server_error_max_seconds", 60))
                        wait = min(max_sleep, base * (2 ** attempt))
                        self.api_manager.jitter_sleep(wait, wait + base, reason=f"SearchTimeline HTTP {response.status_code}")
                        continue
                    return {"_failure": self._classify_http_failure(int(response.status_code), has_pages, cursor), "_status": int(response.status_code), "_attempts": attempts, "_error_samples": errors[-5:]}
                errors.append({"status_code": int(response.status_code), "cursor": cursor, "attempt": attempt + 1, "response_text": str(response.text or "")[:500]})
                return {"_failure": self._classify_http_failure(int(response.status_code), has_pages, cursor), "_status": int(response.status_code), "_attempts": attempts, "_error_samples": errors[-5:]}
            except Exception as exc:
                errors.append({"cursor": cursor, "attempt": attempt + 1, "exception": str(exc)[:500]})
                request_attempts = int(retry_policy.get("request_error_attempts", self.fetcher.max_cursor_error_retries))
                if attempt < request_attempts - 1:
                    base = float(retry_policy.get("request_error_base_seconds", 5))
                    max_sleep = float(retry_policy.get("request_error_max_seconds", 60))
                    wait = min(max_sleep, base * (2 ** attempt))
                    self.api_manager.jitter_sleep(wait, wait + base, reason="SearchTimeline request error")
                    continue
                return {
                    "_failure": "partial_request_error" if has_pages else "failed_initial_request_error",
                    "_status": None,
                    "_attempts": attempts,
                    "_error_samples": errors[-5:],
                }
        return {
            "_failure": "partial_unknown_error" if has_pages else "failed_initial_unknown_error",
            "_status": None,
            "_attempts": attempts,
            "_error_samples": errors[-5:],
        }

    def _raw_batch_dir(self, slug: str, product: str) -> Path:
        target = self.raw_root / slug / product.lower() / self.storage._jalali_batch_name()
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _save_exports(self, slug: str, product: str, raw_query: str, tweets: List[Dict[str, Any]], debug: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, str]:
        target = self.processed_root / slug / product.lower()
        debug_target = self.debug_root / slug / product.lower()
        target.mkdir(parents=True, exist_ok=True)
        debug_target.mkdir(parents=True, exist_ok=True)
        dedup = {str(tweet.get("id")): tweet for tweet in tweets if tweet.get("id")}
        tweets_sorted = list(dedup.values())
        payload = {"generated_at": datetime.utcnow().isoformat() + "Z", "search_slug": slug, "product": product, "raw_query": raw_query, "metadata": metadata, "tweets": tweets_sorted}
        json_path = target / f"{slug}.json"
        txt_path = target / f"{slug}.txt"
        self._save_json(json_path, payload)
        self.storage.save_processed_txt(tweets_sorted, txt_path)
        for name in ["entry_type_counts", "cursor_candidates", "skipped_entries", "processed_entries"]:
            self._save_json(debug_target / f"{slug}__debug_first_page_{name}.json", debug.get(name, {} if name == "entry_type_counts" else []))
        return {"json": str(json_path), "txt": str(txt_path), "debug_dir": str(debug_target)}

    def _state_key(self, search_def: Dict[str, Any], product: str) -> str:
        return f"{SearchQueryBuilder.slug(search_def)}::{product.lower()}"

    def should_fetch_search(self, search_def: Dict[str, Any], product: str, interval_seconds: int) -> bool:
        state = self.search_state.get(self._state_key(search_def, product), {})
        last = state.get("last_checked_at") if isinstance(state, dict) else None
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", ""))
        except Exception:
            return True
        return (datetime.utcnow() - last_dt).total_seconds() >= interval_seconds

    def monitor_search(self, search_def: Dict[str, Any]) -> Dict[str, Any]:
        product = SearchQueryBuilder.normalize_product(str(search_def.get("product", "Top")))
        slug = SearchQueryBuilder.slug(search_def)
        raw_query = SearchQueryBuilder.build_raw_query(search_def, self.storage._tehran_now())
        search_url = SearchQueryBuilder.build_human_search_url(raw_query, product)
        policy = self._policy_for_search(search_def)
        batch_dir = self._raw_batch_dir(slug, product)
        seen_ids: Set[str] = set()
        cursor: Optional[str] = None
        cursor_history: Set[str] = set()
        tweets: List[Dict[str, Any]] = []
        debug: Dict[str, Any] = {}
        attempts = 0
        error_samples: List[Dict[str, Any]] = []
        last_http_status: Optional[int] = None
        exhausted_reason = "unknown"
        rolling_hours = int(policy["rolling_hours"])
        window_start = datetime.utcnow() - timedelta(hours=max(1, rolling_hours))
        query_id = str(self.api_manager.get_query_id("SearchTimeline") or "").strip()
        if not query_id:
            raise RuntimeError("Missing api_config.search_timeline_query_id")
        graphql_url = f"https://x.com/i/api/graphql/{query_id}/SearchTimeline"
        variables_template = self._build_base_variables(search_def, raw_query, product)
        features_json = self._compact_json(dict(FROZEN_SEARCH_FEATURES))

        self.api_manager.warmup_url(search_url, timeout=int(self.config.get("api_config", {}).get("search_warmup_seconds", 30)))
        if self.fetcher.first_request_warmup_seconds > 0:
            time.sleep(self.fetcher.first_request_warmup_seconds)
        frozen_headers = self._build_frozen_headers(search_url)
        for page in range(1, int(policy["pagination_safety_cap_pages"]) + 1):
            payload = self._request_page(
                graphql_url,
                variables_template,
                features_json,
                frozen_headers,
                cursor,
                int(policy["max_retries"]),
                has_pages=bool(tweets),
            )
            attempts += int(payload.get("_attempts", 0) or 0)
            last_http_status = payload.get("_status", last_http_status)
            error_samples.extend(payload.get("_error_samples", []) if isinstance(payload.get("_error_samples"), list) else [])
            if payload.get("_failure"):
                exhausted_reason = str(payload["_failure"])
                break
            payload.pop("_attempts", None)
            payload.pop("_error_samples", None)
            payload.pop("_status", None)
            self.storage.save_raw_page(batch_dir, page, payload)
            page_result = self._parse_search_page(payload, seen_ids, capture_debug=(page == 1))
            tweets.extend(page_result["tweets"])
            if page == 1:
                debug = {key: page_result.get(key) for key in ["entry_type_counts", "cursor_candidates", "skipped_entries", "processed_entries", "selected_cursor_source"]}
            next_cursor = page_result.get("next_cursor")
            if self._page_crossed_search_window(page_result["tweets"], window_start):
                exhausted_reason = "success_search_window_crossed"
                break
            stall_reason = self.classify_search_stall(
                cursor=cursor,
                next_cursor=next_cursor,
                has_entries=bool(page_result.get("has_entries")),
                new_items_count=len(page_result.get("tweets", [])),
                cursor_history=cursor_history,
            )
            if stall_reason:
                exhausted_reason = stall_reason
                break
            cursor_history.add(str(next_cursor))
            cursor = str(next_cursor)
            self.api_manager.human_delay("between_pages")
        else:
            exhausted_reason = "partial_safety_cap_reached"

        metadata = {
            "pages_requested": int(policy["pagination_safety_cap_pages"]),
            "pages_saved": len(list(batch_dir.glob("page_*.json"))),
            "exhausted_reason": exhausted_reason,
            "cursor_history": sorted(cursor_history),
            "raw_batch_path": str(batch_dir),
            "rolling_hours": rolling_hours,
            "window_start_utc": window_start.isoformat() + "Z",
            "attempts": attempts,
            "last_http_status": last_http_status,
            "error_samples": error_samples[-5:],
        }
        outputs = self._save_exports(slug, product, raw_query, tweets, debug, metadata)
        report = {"search": search_def.get("name", slug), "slug": slug, "product": product, "raw_query": raw_query, "metadata": metadata, "counts": {"tweets": len(tweets)}, "outputs": outputs}
        self.search_state[self._state_key(search_def, product)] = {"last_checked_at": datetime.utcnow().isoformat() + "Z", "last_status": exhausted_reason, "last_counts": report["counts"]}
        self._save_json(self.state_file, self.search_state)
        self._save_json(self.reports_root / f"{slug}_{product.lower()}_{self.storage._jalali_batch_name()}.json", report)
        return report

    def run_cycle(self, only_names: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
        reports = []
        for search_def in self.search_defs:
            if not search_def.get("enabled", True):
                continue
            name = str(search_def.get("name", ""))
            if only_names and name not in only_names and SearchQueryBuilder.slug(search_def) not in only_names:
                continue
            product = SearchQueryBuilder.normalize_product(str(search_def.get("product", "Top")))
            policy = self._policy_for_search(search_def)
            if not self.should_fetch_search(search_def, product, int(policy["poll_interval_seconds"])):
                continue
            reports.append(self.monitor_search(search_def))
        return reports

    def run_continuous(self, only_names: Optional[Set[str]] = None, check_interval: int = 60) -> None:
        print("Starting v4 SearchTimeline monitor. Press Ctrl+C to stop.")
        while True:
            reports = self.run_cycle(only_names=only_names)
            print(f"Search cycle complete: {len(reports)} search(es) fetched")
            time.sleep(max(1, check_interval))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run isolated v4 SearchTimeline monitoring.")
    parser.add_argument("--config", default="shared/config/config.json")
    parser.add_argument("--search-config", default="shared/config/search_config.json")
    parser.add_argument("--only", action="append", help="Limit to search name/slug; can be repeated.")
    parser.add_argument("--once", action="store_true", help="Run one validation cycle instead of continuous mode.")
    parser.add_argument("--check-interval", type=int, default=60)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    monitor = SearchTimelineMonitor(config_path=args.config, search_config_path=args.search_config)
    only = set(args.only or []) or None
    if args.once:
        print(monitor.run_cycle(only_names=only))
    else:
        monitor.run_continuous(only_names=only, check_interval=args.check_interval)


if __name__ == "__main__":
    main()
```

## File: shared/auth/__init__.py
```python
"""Authentication package for session and cookie management."""
```

## File: shared/auth/session_updater.py
```python
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from playwright.sync_api import sync_playwright, Request

logger = logging.getLogger(__name__)

class SessionUpdater:
    """
    مدیریت و به‌روزرسانی پارامترهای احراز هویت توییتر.
    از Playwright برای استخراج x-client-transaction-id و کوکی‌های جدید (ct0) استفاده می‌کند.
    """

    def __init__(self):
        # مسیر فایل کانفیگ بر اساس ساختار پروژه
        self.config_path = Path(__file__).resolve().parents[1] / "config" / "config.json"
        self._target_url = "https://twitter.com/home"
        self._graphql_indicator = "/graphql/"

    def _load_config(self) -> Dict[str, Any]:
        """بارگذاری فایل کانفیگ اصلی."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found at: {self.config_path}")
        with open(self.config_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _save_config(self, config_data: Dict[str, Any]) -> None:
        """ذخیره تغییرات در فایل کانفیگ."""
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
        logger.info(f"Config successfully updated at {self.config_path}")

    def update_session(self) -> bool:
        """
        اجرای مرورگر، تزریق کوکی‌های فعلی، شنود درخواست‌ها و استخراج پارامترهای جدید.
        برمی‌گرداند: True در صورت موفقیت، False در صورت شکست.
        """
        config = self._load_config()
        current_cookies = config.get("api_cookies", {})
        
        # تبدیل کوکی‌های فایل کانفیگ به فرمت Playwright
        playwright_cookies = []
        for name, value in current_cookies.items():
            playwright_cookies.append({
                "name": name,
                "value": str(value),
                "domain": ".twitter.com",
                "path": "/"
            })

        extracted_data = {
            "x-client-transaction-id": None,
            "ct0": None,
            "auth_token": current_cookies.get("auth_token")
        }

        logger.info("Launching Playwright to extract fresh auth parameters...")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                context.add_cookies(playwright_cookies)
                page = context.new_page()

                # تابع شنود (Interceptor)
                def handle_request(request: Request):
                    if self._graphql_indicator in request.url:
                        headers = request.headers
                        if "x-client-transaction-id" in headers and not extracted_data["x-client-transaction-id"]:
                            extracted_data["x-client-transaction-id"] = headers["x-client-transaction-id"]
                            logger.debug("Successfully intercepted x-client-transaction-id.")

                page.on("request", handle_request)
                
                # رفتن به صفحه هوم توییتر برای تریگر شدن ریکوئست‌های GraphQL
                page.goto(self._target_url, wait_until="networkidle", timeout=60000)

                # استخراج کوکی‌های جدید به‌روزرسانی شده توسط مرورگر
                new_cookies = context.cookies()
                for cookie in new_cookies:
                    if cookie["name"] == "ct0":
                        extracted_data["ct0"] = cookie["value"]
                    elif cookie["name"] == "auth_token":
                        extracted_data["auth_token"] = cookie["value"]

                browser.close()

            # بررسی اینکه آیا پارامترهای کلیدی با موفقیت استخراج شده‌اند یا خیر
            if extracted_data["x-client-transaction-id"] and extracted_data["ct0"]:
                logger.info("New authentication parameters extracted successfully.")
                
                # اعمال تغییرات در شیء کانفیگ
                config["api_headers"]["x-client-transaction-id"] = extracted_data["x-client-transaction-id"]
                config["api_cookies"]["ct0"] = extracted_data["ct0"]
                config["api_cookies"]["auth_token"] = extracted_data["auth_token"]
                
                self._save_config(config)
                return True
            else:
                logger.error("Failed to extract complete auth parameters. Manual login may be required.")
                return False

        except Exception as e:
            logger.error(f"Error during session update via Playwright: {e}")
            return False

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    updater = SessionUpdater()
    updater.update_session()
```

## File: shared/auth/setup_api_cookies.py
```python
#!/usr/bin/env python3
"""
Twitter API Configuration Setup

This script helps you configure the Twitter scraper with your browser cookies
and API parameters.

WHAT YOU NEED:
1. Browser cookies from x.com (while logged in)
2. Bearer token (from Network tab)
3. Query IDs (from Network tab - optional, has defaults)

HOW TO GET THEM:
See CONFIG_GUIDE.md for detailed step-by-step instructions.

QUICK START:
1. Log in to x.com in your browser
2. Open DevTools (F12) → Application → Cookies
3. Copy all cookie values
4. Run this script and paste them when prompted
"""

import json
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
DEFAULT_TRANSACTION_ID = "Z5LW13y5+ps/Uciw/RJPTIXvJyhzpjV0wZBBgOeWohjwEMG2A0pgaN8s11s5Zq8R02R7y2Iv4XAluvl04DPn8bDWVCapZA"

ENDPOINT_KEY_MAP = {
    "UserByScreenName": "user_by_screen_name_query_id",
    "UserTweets": "user_tweets_query_id",
    "UserTweetsAndReplies": "user_tweets_and_replies_query_id",
    "TweetDetail": "tweet_detail_query_id",
    "SearchTimeline": "search_timeline_query_id",
}


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "shared" / "config" / "config.json"


def _extract_query_id_from_url(url: str):
    """Extract (endpoint, query_id) from a GraphQL request URL."""
    try:
        parsed = urlparse(url.strip())
        path = parsed.path or ""
        # Expected: /i/api/graphql/{QUERY_ID}/{ENDPOINT}
        parts = [p for p in path.split("/") if p]
        if "graphql" not in parts:
            return None, None
        graph_idx = parts.index("graphql")
        if len(parts) <= graph_idx + 2:
            return None, None

        query_id = parts[graph_idx + 1]
        endpoint = parts[graph_idx + 2]

        if endpoint in ENDPOINT_KEY_MAP and query_id:
            return endpoint, query_id
    except Exception:
        return None, None
    return None, None

def extract_query_ids_from_har(har_file_path):
    """Extract query IDs from HAR (HTTP Archive) file exported from browser."""
    import json
    
    try:
        with open(har_file_path, 'r') as f:
            har_data = json.load(f)
        
        query_ids = {}
        
        # Parse HAR entries
        for entry in har_data.get('log', {}).get('entries', []):
            url = entry.get('request', {}).get('url', '')
            endpoint, query_id = _extract_query_id_from_url(url)
            if endpoint and query_id:
                query_ids[ENDPOINT_KEY_MAP[endpoint]] = query_id
        
        return query_ids
    except Exception as e:
        print(f"✗ Error parsing HAR file: {e}")
        return {}

def extract_query_ids_from_text(text):
    """Extract query IDs from pasted network URLs."""
    import re
    
    query_ids = {}

    # Broad URL matcher so pasted DevTools dumps are supported.
    candidate_urls = re.findall(r'https?://[^\s"\'<>]+', text)
    for url in candidate_urls:
        endpoint, query_id = _extract_query_id_from_url(url)
        if endpoint and query_id:
            query_ids[ENDPOINT_KEY_MAP[endpoint]] = query_id
    
    return query_ids

def setup_query_ids_auto():
    """Interactive setup for automatic query ID extraction."""
    print("\n" + "=" * 70)
    print("Automatic Query ID Extraction")
    print("=" * 70)
    print("\nChoose method:")
    print("  1. Paste network URLs from browser DevTools")
    print("  2. Import HAR file (File → Save all as HAR)")
    print("  3. Manual entry")
    print()
    
    choice = input("Choice (1/2/3): ").strip()
    
    if choice == '1':
        print("\nPaste network URLs (one or more lines):")
        print("Example: https://x.com/i/api/graphql/6eh3huj6fJnA3Naupj4w0Q/UserTweetsAndReplies?...")
        print("Example: https://x.com/i/api/graphql/QUERY_ID/TweetDetail?...")
        print("(Press Ctrl+D or Ctrl+Z when done)")
        print()
        
        lines = []
        try:
            while True:
                line = input()
                lines.append(line)
        except EOFError:
            pass
        
        text = '\n'.join(lines)
        query_ids = extract_query_ids_from_text(text)
        
        if query_ids:
            print(f"\n✓ Extracted {len(query_ids)} query IDs:")
            for key, value in query_ids.items():
                endpoint = key.replace('_query_id', '').replace('_', ' ').title()
                print(f"  - {endpoint}: {value}")
            return query_ids
        else:
            print("\n✗ No query IDs found in pasted text")
            return None
    
    elif choice == '2':
        har_path = input("\nEnter path to HAR file: ").strip()
        query_ids = extract_query_ids_from_har(har_path)
        
        if query_ids:
            print(f"\n✓ Extracted {len(query_ids)} query IDs from HAR file")
            for key, value in query_ids.items():
                endpoint = key.replace('_query_id', '').replace('_', ' ').title()
                print(f"  - {endpoint}: {value}")
            return query_ids
        else:
            print("\n✗ No query IDs found in HAR file")
            return None
    
    else:
        return None




def setup_cookies():
    """Interactive setup for API cookies and configuration."""
    print("=" * 70)
    print("Twitter API Configuration Setup")
    print("=" * 70)
    print("\nThis script will help you configure:")
    print("  1. API Cookies (required)")
    print("  2. Bearer Token (required)")
    print("  3. Query IDs (optional - has defaults)")
    print("\n" + "=" * 70)
    
    config_file = CONFIG_FILE
    
    # Check if config exists
    if config_file.exists():
        print("\n✓ Found existing config file")
        with open(config_file, 'r') as f:
            config = json.load(f)
        print(f"  - Cookies: {len(config.get('api_cookies', {}))} cookies configured")
        print(f"  - Bearer token: {'✓' if config.get('api_auth', {}).get('bearer_token') else '✗'}")
        print(f"  - Extra API headers: {len(config.get('api_headers', {}))} configured")
        print(f"  - Query IDs: {'✓' if config.get('api_config') else '✗ (using defaults)'}")
        
        print("\nDo you want to:")
        print("  1. Update cookies only (most common)")
        print("  2. Update everything")
        print("  3. Exit")
        choice = input("\nChoice (1-3): ").strip()
        
        if choice == "3":
            print("\n✓ No changes made")
            return
        elif choice == "1":
            update_cookies_only(config, config_file)
            return
    else:
        config = {}
        print("\n⚠️  No config file found. Creating new configuration...")
    
    # Full setup
    print("\n" + "=" * 70)
    print("STEP 1: Browser Cookies")
    print("=" * 70)
    print("\nHow to get cookies:")
    print("  1. Open x.com in browser (logged in)")
    print("  2. Press F12 → Application tab → Cookies → x.com")
    print("  3. Copy the entire cookie string")
    print("\nExample format:")
    print("  auth_token=abc123; ct0=def456; twid=u%3D789...")
    print()
    
    cookies_str = input("Paste your cookies here: ").strip()
    
    if not cookies_str:
        print("\n✗ No cookies provided. Using existing or defaults.")
        cookies_dict = config.get('api_cookies', {})
    else:
        # Parse cookies
        cookies_dict = {}
        for cookie in cookies_str.split('; '):
            if '=' in cookie:
                key, value = cookie.split('=', 1)
                cookies_dict[key] = value
        print(f"\n✓ Parsed {len(cookies_dict)} cookies")
    
    # Bearer token
    print("\n" + "=" * 70)
    print("STEP 2: Bearer Token")
    print("=" * 70)
    print("\nHow to get bearer token:")
    print("  1. Open x.com → F12 → Network tab")
    print("  2. Filter by 'graphql'")
    print("  3. Click any request → Headers → authorization")
    print("  4. Copy the token after 'Bearer '")
    print("\nDefault (usually works):")
    print(f"  {DEFAULT_BEARER_TOKEN}")
    print()
    
    current_bearer = config.get('api_auth', {}).get('bearer_token', DEFAULT_BEARER_TOKEN)
    bearer_token = input("Bearer token (press Enter to keep current/default): ").strip()
    if not bearer_token:
        bearer_token = current_bearer
        print("✓ Using existing/default bearer token")

    print("\n" + "=" * 70)
    print("STEP 3: Required API Headers")
    print("=" * 70)
    print("\nSome X GraphQL requests require x-client-transaction-id.")
    print("Find it in DevTools → Network → graphql request → Headers.")
    print("Press Enter to keep the existing/default value.")
    print()

    existing_headers = config.get('api_headers', {})
    current_transaction_id = existing_headers.get('x-client-transaction-id', DEFAULT_TRANSACTION_ID)
    transaction_id = input("x-client-transaction-id: ").strip() or current_transaction_id
    api_headers = dict(existing_headers)
    api_headers['x-client-transaction-id'] = transaction_id
    
    # Query IDs
    print("\n" + "=" * 70)
    print("STEP 4: Query IDs (Optional)")
    print("=" * 70)
    print("\nQuery IDs change occasionally. Update only if you get 404 errors.")
    print("Before collecting URLs/HAR, browse these pages in X:")
    print("  1. https://x.com/explore")
    print("  2. https://x.com/<username>")
    print("  3. https://x.com/<username>/with_replies")
    print("  4. https://x.com/<username>/status/<tweet_id>")
    print("\nThis helps capture fresh IDs for:")
    print("  - UserByScreenName")
    print("  - UserTweets")
    print("  - UserTweetsAndReplies")
    print("  - TweetDetail")
    print("  - SearchTimeline")
    print("See CONFIG_GUIDE.md for how to find them in Network tab.")
    print()
    
    update_query_ids = input("Update query IDs? (y/N): ").strip().lower()
    existing_api_config = config.get('api_config', {})
    
    if update_query_ids == 'y':
        # Try automatic extraction first
        try:
            auto_ids = setup_query_ids_auto()
        except Exception as e:
            print(f"\n⚠️  Automatic extraction failed: {e}")
            print("Falling back to manual entry...")
            auto_ids = None
        
        if auto_ids:
            # Use automatically extracted IDs
            api_config = dict(existing_api_config)
            api_config.update(auto_ids)
        else:
            # Fallback to manual entry
            print("\nEnter query IDs manually (press Enter to keep current/default):")
            
            current_user_by_screen_name = existing_api_config.get("user_by_screen_name_query_id", "sLVLhk0bGj3MVFEKTdax1w")
            current_user_tweets = existing_api_config.get("user_tweets_query_id", "naBcZ4al-iTCFBYGOAMzBQ")
            current_user_tweets_replies = existing_api_config.get("user_tweets_and_replies_query_id", "6eh3huj6fJnA3Naupj4w0Q")
            current_tweet_detail = existing_api_config.get("tweet_detail_query_id", "")
            current_search_timeline = existing_api_config.get("search_timeline_query_id", "")
            user_by_screen_name = input(f"  UserByScreenName [{current_user_by_screen_name}]: ").strip()
            user_tweets = input(f"  UserTweets [{current_user_tweets}]: ").strip()
            user_tweets_replies = input(f"  UserTweetsAndReplies [{current_user_tweets_replies}]: ").strip()
            tweet_detail = input(f"  TweetDetail [{current_tweet_detail}]: ").strip()
            search_timeline = input(f"  SearchTimeline [{current_search_timeline}]: ").strip()
            
            api_config = dict(existing_api_config)
            api_config.update({
                "user_by_screen_name_query_id": user_by_screen_name or current_user_by_screen_name,
                "user_tweets_query_id": user_tweets or current_user_tweets,
                "user_tweets_and_replies_query_id": user_tweets_replies or current_user_tweets_replies,
                "tweet_detail_query_id": tweet_detail or current_tweet_detail,
                "search_timeline_query_id": search_timeline or current_search_timeline,
            })
    else:
        # Use existing or defaults
        api_config = existing_api_config or {
            "user_by_screen_name_query_id": "sLVLhk0bGj3MVFEKTdax1w",
            "user_tweets_query_id": "naBcZ4al-iTCFBYGOAMzBQ",
            "user_tweets_and_replies_query_id": "6eh3huj6fJnA3Naupj4w0Q",
            "tweet_detail_query_id": "",
            "search_timeline_query_id": "",
        }
        print("✓ Using existing/default query IDs")

    api_config.setdefault("search_timeline_query_id", "")
    
    # Build final config
    config['api_cookies'] = cookies_dict
    config['api_auth'] = {'bearer_token': bearer_token}
    config['api_headers'] = api_headers
    config['api_config'] = api_config
    config.setdefault('anti_bot_simulation', {
        "enabled": True,
        "browse_warmup_enabled": True,
        "warmup_pages": 2,
        "delays_seconds": {
            "before_request_min": 0.2,
            "before_request_max": 1.2,
            "between_requests_min": 0.5,
            "between_requests_max": 2.5,
            "between_pages_min": 2,
            "between_pages_max": 6,
            "replies_retry_min": 1,
            "replies_retry_max": 3,
            "between_accounts_min": 3,
            "between_accounts_max": 8,
            "between_cycles_min": 0,
            "between_cycles_max": 60
        }
    })
    
    # Save
    config_file.parent.mkdir(exist_ok=True)
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)
    
    print("\n" + "=" * 70)
    print("✅ Configuration saved!")
    print("=" * 70)
    print(f"\nSaved to: {config_file}")
    print("\nConfiguration summary:")
    print(f"  - Cookies: {len(cookies_dict)} configured")
    print(f"  - Bearer token: {bearer_token[:50]}...")
    print(f"  - Extra API headers: {len(api_headers)} configured")
    print(f"  - Query IDs: {len(api_config)} configured")
    
    print("\n" + "=" * 70)
    print("Next steps:")
    print("=" * 70)
    print("  1. Run: python3 historical_runner.py (or live_runner.py / search_runner.py)")
    print("  2. If you get errors, see CONFIG_GUIDE.md")
    print("  3. Update cookies when they expire (every 30-90 days)")
    print()

def update_cookies_only(config, config_file):
    """Quick update for cookies only."""
    print("\n" + "=" * 70)
    print("Update Cookies Only")
    print("=" * 70)
    print("\nPaste your new cookies (from browser DevTools):")
    print("Format: auth_token=abc; ct0=def; twid=ghi...")
    print()
    
    cookies_str = input("Cookies: ").strip()
    
    if not cookies_str:
        print("\n✗ No cookies provided. No changes made.")
        return
    
    # Parse cookies
    cookies_dict = {}
    for cookie in cookies_str.split('; '):
        if '=' in cookie:
            key, value = cookie.split('=', 1)
            cookies_dict[key] = value
    
    config['api_cookies'] = cookies_dict
    config.setdefault('api_headers', {})
    config['api_headers'].setdefault('x-client-transaction-id', DEFAULT_TRANSACTION_ID)
    
    # Save
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)
    
    print(f"\n✓ Updated {len(cookies_dict)} cookies")
    print(f"✓ Saved to: {config_file}")
    print("\n✅ Ready to run your runner scripts (e.g., historical_runner.py)")

if __name__ == "__main__":
    try:
        setup_cookies()
    except KeyboardInterrupt:
        print("\n\n✗ Setup cancelled")
    except Exception as e:
        print(f"\n✗ Error: {e}")
        print("\nFor help, see CONFIG_GUIDE.md")
```

## File: shared/config/__init__.py
```python
"""Configuration package for scraper settings and tier policies."""
```

## File: shared/config/config.json
```json
{
  "api_cookies": {
    "guest_id": "v1%3A177711975217751018",
    "__cuid": "371bef68d36541d08b0f1992cf805185",
    "g_state": "{\"i_l\":0,\"i_ll\":1777148016989,\"i_e\":{\"enable_itp_optimization\":0},\"i_et\":1777147970292}",
    "kdt": "lN34hDtC2uXY0wtApwoiRPvF4D4QrZZnl3qEquR0",
    "auth_token": "df51ac6bd02c2cc631982c57c7f175cb650e18a4",
    "ct0": "1bbc00f49ff258c217c809e5e1db70be507b05c51065fe5c280cd9f731c01cfc95d4c271995ab46486cfd175d958b0e59b6ae36b419cc2a603b52111dcd6468ee6d12cbe9a11180319729ddf912e198c",
    "twid": "u%3D1076002579962871808",
    "d_prefs": "MToxLGNvbnNlbnRfdmVyc2lvbjoyLHRleHRfdmVyc2lvbjoxMDAw",
    "guest_id_ads": "v1%3A177711975217751018",
    "guest_id_marketing": "v1%3A177711975217751018",
    "personalization_id": "\"v1_nRj8IpZT3VVqxpbeudDDBA==\"",
    "lang": "en",
    "__cf_bm": "9sXNBRw1reVH40n19oO0t5XhKN9CYIKOrUlCmLtzHAI-1779087175.0339284-1.0.1.1-n24ZcGAHNoo001N3G2nObjrYBy0B5pAYh2_0c.nZCcPSMjYX6japquP3YXS3s0CXwEYoLzfQkGgFPhEwqFk1aRgUlDjQzoJWpYr6V3ayMDYGLl0c0C6LwwiU6m7lau4_"
  },
  "api_auth": {
    "bearer_token": "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
  },
  "api_headers": {
    "x-client-transaction-id": "6321pPLyLpdTOrt3rX+dSBC8BX0KezdqozlXSdolUT7Gma1+27/++/9aXoPAfmKYG5a7P+4JM2oq2d88dn+M46kHbCBS6A"
  },
  "api_config": {
    "user_by_screen_name_query_id": "sLVLhk0bGj3MVFEKTdax1w",
    "user_tweets_query_id": "pQHADmT91zIY83UbK0x4Lw",
    "user_tweets_and_replies_query_id": "xdqXQQg4vOBF9Np6VtUsdw",
    "cursor_error_max_retries": 3,
    "default_timeout_seconds": 20,
    "tweet_detail_query_id": "",
    "search_timeline_query_id": "099UqLkXma7fhT81Jv4n9g",
    "search_warmup_seconds": 2,
    "first_request_warmup_seconds": 15,
    "pagination_safety_cap_pages": 50
  },
  "graphql_endpoint_payloads": {
    "UserTweetsAndReplies": {
      "variables": {
        "initial": {
          "userId": "{user_id}",
          "count": 20,
          "includePromotedContent": true,
          "withCommunity": true,
          "withVoice": true
        },
        "pagination": {
          "userId": "{user_id}",
          "count": 20,
          "cursor": "{cursor}",
          "includePromotedContent": true,
          "withCommunity": true,
          "withVoice": true
        }
      },
      "features": {
        "rweb_video_screen_enabled": false,
        "rweb_cashtags_enabled": true,
        "profile_label_improvements_pcf_label_in_post_enabled": true,
        "responsive_web_profile_redirect_enabled": false,
        "rweb_tipjar_consumption_enabled": false,
        "verified_phone_label_enabled": false,
        "creator_subscriptions_tweet_preview_api_enabled": true,
        "responsive_web_graphql_timeline_navigation_enabled": true,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": false,
        "premium_content_api_read_enabled": false,
        "communities_web_enable_tweet_community_results_fetch": true,
        "c9s_tweet_anatomy_moderator_badge_enabled": true,
        "responsive_web_grok_analyze_button_fetch_trends_enabled": false,
        "responsive_web_grok_analyze_post_followups_enabled": true,
        "rweb_cashtags_composer_attachment_enabled": true,
        "responsive_web_jetfuel_frame": true,
        "responsive_web_grok_share_attachment_enabled": true,
        "responsive_web_grok_annotations_enabled": true,
        "articles_preview_enabled": true,
        "responsive_web_edit_tweet_api_enabled": true,
        "rweb_conversational_replies_downvote_enabled": false,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": true,
        "view_counts_everywhere_api_enabled": true,
        "longform_notetweets_consumption_enabled": true,
        "responsive_web_twitter_article_tweet_consumption_enabled": true,
        "content_disclosure_indicator_enabled": true,
        "content_disclosure_ai_generated_indicator_enabled": true,
        "responsive_web_grok_show_grok_translated_post": true,
        "responsive_web_grok_analysis_button_from_backend": true,
        "post_ctas_fetch_enabled": true,
        "freedom_of_speech_not_reach_fetch_enabled": true,
        "standardized_nudges_misinfo": true,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": true,
        "longform_notetweets_rich_text_read_enabled": true,
        "longform_notetweets_inline_media_enabled": false,
        "responsive_web_grok_image_annotation_enabled": true,
        "responsive_web_grok_imagine_annotation_enabled": true,
        "responsive_web_grok_community_note_auto_translation_is_enabled": true,
        "responsive_web_enhance_cards_enabled": false
      },
      "fieldToggles": {
        "withArticlePlainText": false
      }
    },
    "SearchTimeline": {
      "features": {
        "rweb_video_screen_enabled": false,
        "rweb_cashtags_enabled": true,
        "profile_label_improvements_pcf_label_in_post_enabled": true,
        "responsive_web_profile_redirect_enabled": false,
        "rweb_tipjar_consumption_enabled": false,
        "verified_phone_label_enabled": false,
        "creator_subscriptions_tweet_preview_api_enabled": true,
        "responsive_web_graphql_timeline_navigation_enabled": true,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": false,
        "premium_content_api_read_enabled": false,
        "communities_web_enable_tweet_community_results_fetch": true,
        "c9s_tweet_anatomy_moderator_badge_enabled": true,
        "responsive_web_grok_analyze_button_fetch_trends_enabled": false,
        "responsive_web_grok_analyze_post_followups_enabled": true,
        "rweb_cashtags_composer_attachment_enabled": true,
        "responsive_web_jetfuel_frame": true,
        "responsive_web_grok_share_attachment_enabled": true,
        "responsive_web_grok_annotations_enabled": true,
        "articles_preview_enabled": true,
        "responsive_web_edit_tweet_api_enabled": true,
        "rweb_conversational_replies_downvote_enabled": false,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": true,
        "view_counts_everywhere_api_enabled": true,
        "longform_notetweets_consumption_enabled": true,
        "responsive_web_twitter_article_tweet_consumption_enabled": true,
        "content_disclosure_indicator_enabled": true,
        "content_disclosure_ai_generated_indicator_enabled": true,
        "responsive_web_grok_show_grok_translated_post": true,
        "responsive_web_grok_analysis_button_from_backend": true,
        "post_ctas_fetch_enabled": true,
        "freedom_of_speech_not_reach_fetch_enabled": true,
        "standardized_nudges_misinfo": true,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": true,
        "longform_notetweets_rich_text_read_enabled": true,
        "longform_notetweets_inline_media_enabled": false,
        "responsive_web_grok_image_annotation_enabled": true,
        "responsive_web_grok_imagine_annotation_enabled": true,
        "responsive_web_grok_community_note_auto_translation_is_enabled": true,
        "responsive_web_enhance_cards_enabled": false
      },
      "fieldToggles": {},
      "variables": {
        "initial": {
          "rawQuery": "{raw_query}",
          "count": 20,
          "querySource": "typed_query",
          "product": "Top",
          "withGrokTranslatedBio": true,
          "withQuickPromoteEligibilityTweetFields": false
        },
        "pagination": {
          "rawQuery": "{raw_query}",
          "count": 20,
          "cursor": "{cursor}",
          "querySource": "typed_query",
          "product": "Top",
          "withGrokTranslatedBio": true,
          "withQuickPromoteEligibilityTweetFields": false
        }
      }
    }
  },
  "rate_limits": {
    "UserByScreenName": {
      "limit": 150,
      "window_seconds": 900
    },
    "UserTweets": {
      "limit": 50,
      "window_seconds": 900
    },
    "UserTweetsAndReplies": {
      "limit": 500,
      "window_seconds": 900
    },
    "TweetDetail": {
      "limit": 150,
      "window_seconds": 900
    },
    "SearchTimeline": {
      "limit": 50,
      "window_seconds": 900
    }
  },
  "anti_bot_simulation": {
    "enabled": true,
    "browse_warmup_enabled": true,
    "warmup_pages": 2,
    "error_retry_policy": {
      "client_error_attempts": 3,
      "client_error_min_seconds": 10,
      "client_error_max_seconds": 20,
      "server_error_attempts": 3,
      "server_error_base_seconds": 5,
      "server_error_max_seconds": 60,
      "request_error_attempts": 3,
      "request_error_base_seconds": 5,
      "request_error_max_seconds": 60,
      "rate_limit_safety_buffer_seconds": 5,
      "max_rate_limit_sleep_seconds": 900
    },
    "delays_seconds": {
      "before_request_min": 0.2,
      "before_request_max": 1.2,
      "between_requests_min": 0.5,
      "between_requests_max": 2.5,
      "between_pages_min": 2,
      "between_pages_max": 6,
      "replies_retry_min": 1,
      "replies_retry_max": 3,
      "between_accounts_min": 3,
      "between_accounts_max": 8,
      "between_cycles_min": 0,
      "between_cycles_max": 60
    }
  },
  "search_timeline_feature_overrides": {},
  "viral_detection": {
    "window_days": 7,
    "threshold_percentile": 95,
    "history_score_weight": 0.3,
    "delta_score_weight": 0.7,
    "composite_score_cutoff": 1.0,
    "delta_percentile_cutoff": 0.8,
    "snapshot_min_metric_delta": 25,
    "snapshot_min_minutes": 10
  }
}
```

## File: shared/config/search_config.json
```json
[
  {
    "name": "Iran_War_Brent_Gold_Inflation_Hormuz",
    "slug": "iran_war_brent_gold_inflation_hormuz",
    "enabled": true,
    "product": "Top",
    "preserve_exact_query": true,
    "raw_query": "(Iran OR War OR Brent OR Gold OR Inflation OR Hormuz) lang:en min_replies:100 min_faves:1000 min_retweets:50 since:2026-05-23",
    "polling_priority": 2,
    "pagination_depth": 3,
    "max_retries": 3,
    "rolling_hours": 24,
    "include_keywords": [
      "Iran",
      "War",
      "Brent",
      "Gold",
      "Inflation",
      "Hormuz"
    ],
    "exact_phrases": [],
    "exclude_keywords": [],
    "from_accounts": [],
    "to_accounts": [],
    "mentions": [],
    "lang": "en",
    "min_replies": 100,
    "min_faves": 1000,
    "min_retweets": 50,
    "since_days": 7,
    "count": 30
  }
]
```

## File: shared/config/tier_config.py
```python
#!/usr/bin/env python3
"""
Tier and rolling-window policy configuration.

This module keeps account priority metadata and per-priority policies in one
place so historical and live pipelines can share the same scheduling rules.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple


DEFAULT_PRIORITY_POLICIES: Dict[int, Dict] = {
    # Highest priority: fastest checks + largest rolling windows.
    1: {
        "poll_interval_seconds": 120,
        "live_window_hours": 48,
        "historical_window_days": 7,
    },
    2: {
        "poll_interval_seconds": 240,
        "live_window_hours": 36,
        "historical_window_days": 5,
    },
    3: {
        "poll_interval_seconds": 360,
        "live_window_hours": 30,
        "historical_window_days": 4,
    },
    4: {
        "poll_interval_seconds": 540,
        "live_window_hours": 24,
        "historical_window_days": 3,
    },
    5: {
        "poll_interval_seconds": 780,
        "live_window_hours": 20,
        "historical_window_days": 2,
    },
    6: {
        "poll_interval_seconds": 1020,
        "live_window_hours": 16,
        "historical_window_days": 1,
    },
    # Default/fallback priority for uncategorized accounts.
    7: {
        "poll_interval_seconds": 1440,
        "live_window_hours": 12,
        "historical_window_days": 1,
    },
}


DEFAULT_TIER_CONFIGURATION: Dict[str, List[Dict[str, str]]] = {
    "priority_1": [
        {"username": "realDonaldTrump", "display_name": "Donald J. Trump"},
        {"username": "SecScottBessent", "display_name": "Scott Bessent"},
        {"username": "USTreasury", "display_name": "US Treasury"},
        {"username": "JDVance", "display_name": "JD Vance"},
        {"username": "TankerTrackers", "display_name": "TankerTrackers"},
        {"username": "chigrl", "display_name": "Tracy Shuchart"},
        {"username": "KobeissiLetter", "display_name": "The Kobeissi Letter"},
        {"username": "AAAnews", "display_name": "AAAnews"},
        {"username": "EIAgov", "display_name": "EIAgov"},
    ],
    "priority_2": [
        {"username": "araghchi", "display_name": "Seyed Abbas Araghchi"},
        {"username": "drpezeshkian", "display_name": "Masoud Pezeshkian"},
        {"username": "MKhamenei_ir", "display_name": "Mojtaba Khamenei"},
        {"username": "SteveWitkoff", "display_name": "Steve Witkoff"},
        {"username": "SecRubio", "display_name": "Marco Rubio"},
        {"username": "LynAldenContact", "display_name": "Lyn Alden"},
        {"username": "LukeGromen", "display_name": "Luke Gromen"},
        {"username": "PeterSchiff", "display_name": "Peter Schiff"},
        {"username": "JimRickards", "display_name": "Jim Rickards"},
        {"username": "business", "display_name": "Bloomberg"},
        {"username": "Reuters", "display_name": "Reuters"},
        {"username": "elonmusk", "display_name": "Elon Musk"},
        {"username": "Lagarde", "display_name": "Christine Lagarde"},
    ],
    "priority_3": [
        {"username": "IRIMFA_SPOX", "display_name": "Esmaeil Baqaei"},
        {"username": "IRIMFA_EN", "display_name": "Iran Foreign Ministry"},
        {"username": "Hemmati_ir", "display_name": "Abdolnaser Hemmati"},
        {"username": "netanyahu", "display_name": "Benjamin Netanyahu"},
        {"username": "Israel_katz", "display_name": "Israel Katz"},
        {"username": "UANI", "display_name": "UANI"},
        {"username": "farnazfassihi", "display_name": "Farnaz Fassihi"},
        {"username": "mdubowitz", "display_name": "Mark Dubowitz"},
        {"username": "rich_goldberg", "display_name": "Richard Goldberg"},
        {"username": "SGhasseminejad", "display_name": "Saeed Ghasseminejad"},
    ],
    "priority_4": [
        {"username": "J_Zarif", "display_name": "J Zarif"},
    ],
    "priority_5": [
        {"username": "IDF", "display_name": "Israel Defense Forces"},
        {"username": "IDFFarsi", "display_name": "IDF Farsi"},
        {"username": "AvichayAdraee", "display_name": "Avichay Adraee"},
        {"username": "DAVIDHALBRIGHT1", "display_name": "David Albright"},
        {"username": "TheGoodISIS", "display_name": "Inst for Science"},
        {"username": "geoconfirmed", "display_name": "GeoConfirmed"},
        {"username": "ronenbergman", "display_name": "Ronen Bergman"},
        {"username": "AmosHarel", "display_name": "Amos Harel"},
        {"username": "ksadjadpour", "display_name": "Karim Sadjadpour"},
        {"username": "vali_nasr", "display_name": "Vali Nasr"},
        {"username": "AliVaez", "display_name": "Ali Vaez"},
    ],
    "priority_6": [
        {"username": "SEPeaceMissions", "display_name": "SE Peace Missions"},
        {"username": "PressSec", "display_name": "Karoline Leavitt"},
        {"username": "gidonsaar", "display_name": "Gideon Saar"},
        {"username": "Shayan86", "display_name": "Shayan Sardarizadeh"},
        {"username": "bellingcat", "display_name": "Bellingcat"},
        {"username": "LauraSecor", "display_name": "Laura Secor"},
        {"username": "MaloneySuzanne", "display_name": "Suzanne Maloney"},
        {"username": "IranIntl", "display_name": "Iran International"},
    ],
    "priority_7": [
        {"username": "BBCVerify", "display_name": "BBC Verify"},
        {"username": "bentallblu", "display_name": "bentallblu"},
        {"username": "HollyDagres", "display_name": "Holly Dagres"},
        {"username": "PahlaviReza", "display_name": "Reza Pahlavi"},
        {"username": "WGC_News", "display_name": "WGC News"},
        {"username": "SantiagoAuFund", "display_name": "Santiago Capital"},
        {"username": "paulkrugman", "display_name": "Paul Krugman"},
        {"username": "RobinBrooksIIF", "display_name": "Robin Brooks"},
        {"username": "elerianm", "display_name": "Mohamed El-Erian"},
        {"username": "NickTimiraos", "display_name": "Nick Timiraos"},
        {"username": "federalreserve", "display_name": "Federal Reserve"},
        {"username": "KitcoNewsNOW", "display_name": "Kitco News"},
        {"username": "GoldTelegraph_", "display_name": "Gold Telegraph"},
        {"username": "flightradar24", "display_name": "Flightradar24"},
        {"username": "RayDalio", "display_name": "Ray Dalio"},
        {"username": "PolymarketIntel", "display_name": "Polymarket Intel"},
    ],
}


def _priority_from_key(key: str) -> int:
    if not key.startswith("priority_"):
        return 7
    try:
        return int(key.split("_", 1)[1])
    except (ValueError, IndexError):
        return 7


def load_tier_config(config: Dict) -> Tuple[Dict[str, Dict], Dict[int, Dict]]:
    """
    Build account->metadata map plus priority policy map.

    Backward compatibility:
    - If `tier_configuration` is absent in config, use module defaults.
    - If some priorities are missing policy overrides, defaults are used.
    """
    tier_cfg = config.get("tier_configuration", DEFAULT_TIER_CONFIGURATION)
    policy_cfg = config.get("priority_policies", {})

    policy_map: Dict[int, Dict] = {}
    for priority, defaults in DEFAULT_PRIORITY_POLICIES.items():
        override = policy_cfg.get(str(priority), {}) or {}
        policy_map[priority] = {
            "priority": priority,
            "poll_interval_seconds": int(override.get("poll_interval_seconds", defaults["poll_interval_seconds"])),
            "live_window_hours": int(override.get("live_window_hours", defaults["live_window_hours"])),
            "historical_window_days": int(override.get("historical_window_days", defaults["historical_window_days"])),
        }

    account_map: Dict[str, Dict] = {}
    for key, records in tier_cfg.items():
        priority = _priority_from_key(key)
        if priority not in policy_map:
            priority = 7
        for record in records or []:
            username = str(record.get("username", "")).strip()
            if not username:
                continue
            display_name = str(record.get("display_name") or username).strip() or username
            account_map[username.lower()] = {
                "username": username,
                "display_name": display_name,
                "priority": priority,
            }

    return account_map, policy_map


def get_priority_policy(
    username: str,
    account_map: Dict[str, Dict],
    policy_map: Dict[int, Dict],
) -> Dict:
    """Return policy for username with priority-7 fallback."""
    meta = account_map.get(username.lower())
    priority = meta.get("priority", 7) if meta else 7
    policy = dict(policy_map.get(priority, policy_map[7]))
    policy["username"] = meta.get("username", username) if meta else username
    policy["display_name"] = meta.get("display_name", username) if meta else username
    policy["priority"] = priority
    return policy


def ordered_accounts(account_map: Dict[str, Dict]) -> List[str]:
    """Return usernames by priority while preserving configured order within each tier."""
    rows = list(account_map.values())
    rows.sort(key=lambda row: int(row.get("priority", 7)))
    return [row["username"] for row in rows if row.get("username")]


class TierConfig:
    """Compatibility wrapper for loading tier account and policy configuration."""

    def __init__(self, config_path: Optional[str] = None, config: Optional[Dict] = None):
        if config is None:
            if config_path:
                path = Path(config_path)
                if not path.is_absolute():
                    # اگر مسیری داده شد، آن را نسبت به ریشه پروژه (parents[2]) پیدا کن
                    path = Path(__file__).resolve().parents[2] / path
            else:
                # اگر مسیری داده نشد، فایل config.json را در همان پوشه فعلی پیدا کن
                path = Path(__file__).resolve().parent / "config.json"
                
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    config = json.load(f)
            else:
                config = {}
                
        self.config = config
        self.account_map, self.policy_map = load_tier_config(config)

    @property
    def accounts(self) -> List[str]:
        return ordered_accounts(self.account_map)

    def get_policy(self, username: str) -> Dict:
        return get_priority_policy(username, self.account_map, self.policy_map)

    def __repr__(self) -> str:
        return f"TierConfig(accounts={len(self.account_map)}, policies={len(self.policy_map)})"
```

## File: shared/core/__init__.py
```python
"""Core API and fetching engine package."""
```

## File: shared/core/api_manager.py
```python
#!/usr/bin/env python3
"""
API Manager - Centralized networking and endpoint management

Responsibilities:
- Session management
- Authentication (cookies, bearer, CSRF)
- Query ID management with auto-refresh detection
- Per-endpoint rate limit tracking
- Retry logic with exponential backoff
- Request accounting and budgeting
- Endpoint health monitoring
"""

import json
import time
import uuid
import base64
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util import Retry
except ImportError:
    print("ERROR: Missing requests. Run: pip3 install requests")
    raise


class EndpointHealth:
    """Track endpoint health status"""
    HEALTHY = "healthy"
    STALE_QUERY_ID = "stale_query_id"
    CONTEXT_REJECTED = "context_rejected"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    UNKNOWN_ERROR = "unknown_error"


@dataclass(frozen=True)
class RequestContext:
    """
    Route-aware browser context for one API request.

    X derives several request headers from the current route/runtime state. Keep
    these values request-scoped so retries can vary context without mutating the
    long-lived authenticated session.
    """
    name: str
    endpoint: str
    referer: str
    active_user: str = "yes"
    warmup_routes: Tuple[str, ...] = ()


class APIManager:
    """Centralized API communication manager"""
    
    # Request costs per endpoint (for budget accounting)
    REQUEST_COSTS = {
        "UserByScreenName": 1,
        "UserTweets": 2,
        "UserTweetsAndReplies": 3,
        "TweetDetail": 2,
        "SearchTimeline": 2,
    }
    
    # Default rate limits (overridden by config)
    DEFAULT_LIMITS = {
        "UserByScreenName": {"limit": 150, "window_seconds": 900},
        "UserTweets": {"limit": 50, "window_seconds": 900},
        "UserTweetsAndReplies": {"limit": 500, "window_seconds": 900},
        "TweetDetail": {"limit": 150, "window_seconds": 900},
        "SearchTimeline": {"limit": 50, "window_seconds": 900},
    }

    # مسیر پیش‌فرض کانفیگ اصلاح شد
    def __init__(self, config_path: str = "shared/config/config.json", state_dir: Optional[Path] = None):
        # با فرض قرارگیری در shared/core/ یا shared/network/ استفاده از parents[2] صحیح است
        project_root = Path(__file__).resolve().parents[2]
        path_obj = Path(config_path)
        if not path_obj.is_absolute():
            path_obj = project_root / path_obj
        self.config_path = path_obj
        self.config = self._load_config()
        self.simulation_config = self.config.get("anti_bot_simulation", {})
        self.default_timeout = int(
            self.config.get("api_config", {}).get("default_timeout_seconds", 20)
        )
        
        # State directory for persistent tracking
        self.state_dir = state_dir or (project_root / "data" / "historical_live" / "state")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.shared_state_dir = self.state_dir
        self.shared_state_dir.mkdir(parents=True, exist_ok=True)
        
        # Session setup
        self.session = requests.Session()
        self._setup_session()
        
        # Rate limit tracking per endpoint
        self.rate_limits: Dict[str, Dict] = self._load_rate_limits()
        
        # Endpoint health tracking
        self.endpoint_health: Dict[str, str] = self._load_endpoint_health()
        self.last_status_by_endpoint: Dict[str, Optional[int]] = {}
        
        # Query IDs
        self.query_ids = self._load_query_ids()

        # Request accounting
        self.request_count = 0
        self.session_start = time.time()
        
    def _load_config(self) -> dict:
        """Load configuration from JSON"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config not found: {self.config_path}")
        with open(self.config_path) as f:
            return json.load(f)
    
    def _setup_session(self):
        """Configure session with auth and headers"""
        # Cookies
        cookies = self.config.get("api_cookies", {})
        for key, value in cookies.items():
            self.session.cookies.set(key, value, domain=".x.com")
        
        # Bearer token
        bearer = self.config.get("api_auth", {}).get("bearer_token", "")
        
        # CSRF token from cookies
        csrf_token = cookies.get("ct0", "")
        
        configured_headers = self.config.get("api_headers", {})
        
        tx_id = configured_headers.get("x-client-transaction-id") or self._generate_transaction_id()

        # Stable browser/session headers copied from the original reliable
        # fetcher. Route-specific requests override only referer/active-user.
        self.session.headers.update({
            "authorization": f"Bearer {bearer}",
            "x-csrf-token": csrf_token,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "x-client-transaction-id": tx_id,
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            "referer": "https://x.com/",
            "accept": "*/*",
            "content-type": "application/json",
            "dnt": "1",
            "priority": "u=1, i",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        })
        if configured_headers:
            self.session.headers.update({k: str(v) for k, v in configured_headers.items() if v})
        self._configure_http_adapter()

    def _configure_http_adapter(self):
        """Attach resilient retry strategy for unstable network conditions."""
        retry_strategy = Retry(
            total=5,
            connect=5,
            read=5,
            status=5,
            backoff_factor=2,
            # Keep 429 visible to fetcher code so it can persist rate-limit
            # headers, sleep until reset, and retry the exact same cursor/page.
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=30, pool_maxsize=30)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _get(self, url: str, **kwargs) -> requests.Response:
        """Session GET with global timeout default."""
        kwargs.setdefault("timeout", self.default_timeout)
        return self.session.get(url, **kwargs)
    
    def _generate_transaction_id(self) -> str:
        """Generate one fallback transaction ID for the session."""
        raw = uuid.uuid4().bytes + int(time.time() * 1000).to_bytes(8, 'big')
        return base64.urlsafe_b64encode(raw).decode()[:72]

    def _default_referer(self, endpoint: str, username: Optional[str] = None) -> str:
        if username:
            if endpoint == "UserTweetsAndReplies":
                return f"https://x.com/{username}/with_replies"
            if endpoint in ["UserTweets", "UserByScreenName"]:
                return f"https://x.com/{username}"
        if endpoint == "SearchTimeline":
            return "https://x.com/search?q=twitter&src=typed_query"
        return "https://x.com/"

    def get_context_variants(self, endpoint: str, username: Optional[str] = None) -> List[RequestContext]:
        """Return the small context set from the original reliable fetcher."""
        profile_url = f"https://x.com/{username}" if username else "https://x.com/"
        replies_url = f"{profile_url}/with_replies" if username else "https://x.com/"

        if endpoint == "UserTweetsAndReplies":
            return [
                RequestContext("replies_tab_passive", endpoint, replies_url, "no", (replies_url,)),
                RequestContext("replies_tab_active", endpoint, replies_url, "yes", (replies_url,)),
                RequestContext("home_fallback", endpoint, "https://x.com/", "yes", ()),
            ]

        if endpoint == "UserTweets":
            return [RequestContext("user_profile", endpoint, f"https://x.com/i/user/{username}" if username else profile_url, "yes", ())]

        if endpoint == "UserByScreenName":
            return [
                RequestContext("profile_lookup", endpoint, profile_url, "yes", (profile_url,)),
                RequestContext("home_lookup", endpoint, "https://x.com/", "yes", ("https://x.com/home",)),
            ]

        if endpoint == "SearchTimeline":
            return [
                RequestContext(
                    "search_timeline_main",
                    endpoint,
                    "https://x.com/search?q=twitter&src=typed_query",
                    "yes",
                    ("https://x.com/search?q=twitter&src=typed_query",),
                ),
                RequestContext(
                    "search_timeline_explore",
                    endpoint,
                    "https://x.com/explore",
                    "yes",
                    ("https://x.com/explore",),
                ),
            ]

        return [RequestContext("default", endpoint, self._default_referer(endpoint, username), "yes", ())]

    def _coerce_context(
        self,
        endpoint: str,
        context: Optional[Union[RequestContext, Dict]],
        username: Optional[str] = None,
    ) -> RequestContext:
        if isinstance(context, RequestContext):
            return context
        if isinstance(context, dict):
            return RequestContext(
                name=str(context.get("name", "custom")),
                endpoint=endpoint,
                referer=str(context.get("referer", self._default_referer(endpoint, username))),
                active_user=str(context.get("active_user", context.get("x-twitter-active-user", "yes"))),
                warmup_routes=tuple(context.get("warmup_routes", ())),
            )
        return self.get_context_variants(endpoint, username)[0]

    def _build_request_headers(
        self,
        endpoint: str,
        context: Optional[Union[RequestContext, Dict]] = None,
        username: Optional[str] = None,
        extra_headers: Optional[Dict] = None,
    ) -> Dict:
        ctx = self._coerce_context(endpoint, context, username)
        headers = dict(self.session.headers)
        headers["referer"] = ctx.referer
        headers["x-twitter-active-user"] = ctx.active_user
        if extra_headers:
            headers.update({k: str(v) for k, v in extra_headers.items() if v})
        if endpoint == "UserTweetsAndReplies":
            headers = self._apply_replies_request_profile(headers, username)
        return headers

    def _apply_replies_request_profile(self, headers: Dict, username: Optional[str]) -> Dict:
        """Match the standalone UserTweetsAndReplies diagnostic request shape."""
        cookies = self.config.get("api_cookies", {})
        bearer = self.config.get("api_auth", {}).get("bearer_token", "")
        csrf_token = cookies.get("ct0", "")
        configured_headers = self.config.get("api_headers", {})
        tx_id = configured_headers.get("x-client-transaction-id") or headers.get("x-client-transaction-id")
        referer = f"https://x.com/{username}/with_replies" if username else "https://x.com/"

        cookie_bits = []
        for key in ["auth_token", "ct0", "lang"]:
            value = cookies.get(key)
            if value:
                cookie_bits.append(f"{key}={value}")

        headers.update({
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "en-GB,en;q=0.9,es-ES;q=0.8,es;q=0.7,en-US;q=0.6",
            "authorization": f"Bearer {bearer}",
            "content-type": "application/json",
            "cookie": "; ".join(cookie_bits),
            "dnt": "1",
            "priority": "u=1, i",
            "referer": referer,
            "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "x-csrf-token": csrf_token,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
        })
        if tx_id:
            headers["x-client-transaction-id"] = str(tx_id)
        return headers

    def _human_delay(self, stage: str = "between_requests"):
        """Apply random delay to mimic human pacing."""
        if not self.simulation_config.get("enabled", True):
            return

        delay_map = self.simulation_config.get("delays_seconds", {
            "before_request_min": 0.2,
            "before_request_max": 1.2,
            "between_requests_min": 0.5,
            "between_requests_max": 2.5,
        })

        if stage == "before_request":
            min_d = float(delay_map.get("before_request_min", 0.2))
            max_d = float(delay_map.get("before_request_max", 1.2))
        elif stage == "between_pages":
            min_d = float(delay_map.get("between_pages_min", 2))
            max_d = float(delay_map.get("between_pages_max", 6))
        elif stage == "between_accounts":
            min_d = float(delay_map.get("between_accounts_min", 3))
            max_d = float(delay_map.get("between_accounts_max", 8))
        elif stage == "replies_retry":
            min_d = float(delay_map.get("replies_retry_min", 1))
            max_d = float(delay_map.get("replies_retry_max", 3))
        else:
            min_d = float(delay_map.get("between_requests_min", 0.5))
            max_d = float(delay_map.get("between_requests_max", 2.5))

        if max_d < min_d:
            max_d = min_d
        time.sleep(random.uniform(min_d, max_d))

    def human_delay(self, stage: str = "between_requests"):
        """Public wrapper for configured anti-bot pacing."""
        self._human_delay(stage)

    def jitter_sleep(self, min_seconds: float, max_seconds: float, reason: str = "") -> float:
        """Sleep for a bounded random duration and return the delay used."""
        min_seconds = max(0.0, float(min_seconds))
        max_seconds = max(min_seconds, float(max_seconds))
        delay = random.uniform(min_seconds, max_seconds)
        if reason:
            print(f"⏳ {reason}; sleeping {delay:.1f}s")
        time.sleep(delay)
        return delay

    def retry_policy(self) -> Dict[str, Union[int, float]]:
        """Return configured status-aware retry settings with safe defaults."""
        configured = self.simulation_config.get("error_retry_policy", {})
        policy = configured if isinstance(configured, dict) else {}
        return {
            "client_error_attempts": int(policy.get("client_error_attempts", 3)),
            "client_error_min_seconds": float(policy.get("client_error_min_seconds", 10)),
            "client_error_max_seconds": float(policy.get("client_error_max_seconds", 20)),
            "server_error_attempts": int(policy.get("server_error_attempts", 3)),
            "server_error_base_seconds": float(policy.get("server_error_base_seconds", 5)),
            "server_error_max_seconds": float(policy.get("server_error_max_seconds", 60)),
            "request_error_attempts": int(policy.get("request_error_attempts", 3)),
            "request_error_base_seconds": float(policy.get("request_error_base_seconds", 5)),
            "request_error_max_seconds": float(policy.get("request_error_max_seconds", 60)),
            "rate_limit_safety_buffer_seconds": int(policy.get("rate_limit_safety_buffer_seconds", 5)),
            "max_rate_limit_sleep_seconds": int(policy.get("max_rate_limit_sleep_seconds", 900)),
        }

    def warmup_navigation_context(
        self,
        username: Optional[str] = None,
        endpoint: Optional[str] = None,
        context: Optional[Union[RequestContext, Dict]] = None,
    ):
        """Best-effort warmup matching the original /with_replies behavior."""
        if not self.simulation_config.get("enabled", True):
            return
        if not self.simulation_config.get("browse_warmup_enabled", True):
            return

        ctx = self._coerce_context(endpoint or "UserTweets", context, username)
        warmup_routes = list(ctx.warmup_routes)
        if not warmup_routes and username:
            warmup_routes = [
                f"https://x.com/{username}/with_replies"
                if endpoint == "UserTweetsAndReplies"
                else f"https://x.com/{username}"
            ]
        warmup_pages = int(self.simulation_config.get("warmup_pages", len(warmup_routes) or 1))
        try:
            for warmup_url in warmup_routes[:warmup_pages]:
                self._get(warmup_url)
                self._human_delay("between_requests")
        except requests.exceptions.RequestException:
            return

    def warmup_session(self, username: str) -> bool:
        """
        Human-like warm-up flow:
        1) Visit home page
        2) Visit user profile page
        3) Pin session referer to the user profile
        """
        username = (username or "").strip().lstrip("@")
        if not username:
            return False
        home_url = "https://x.com/"
        profile_url = f"https://x.com/{username}"
        try:
            self._get(home_url)
            self._get(profile_url)
            self.session.headers["referer"] = profile_url
            return True
        except requests.exceptions.RequestException as exc:
            print(f"⚠️  Warm-up navigation failed for @{username}: {exc}")
            return False

    def warmup_user_context(self, username: Optional[str] = None):
        """Backward-compatible profile warmup wrapper."""
        self.warmup_navigation_context(username=username, endpoint="UserTweets")

    def warmup_url(self, url: str, timeout: int = 30):
        """Best-effort warmup for non-profile routes (e.g., search pages)."""
        if not self.simulation_config.get("enabled", True):
            return
        if not self.simulation_config.get("browse_warmup_enabled", True):
            return
        target = str(url or "").strip()
        if not target:
            return
        try:
            self._get(target, timeout=timeout)
        except requests.exceptions.RequestException:
            return

    def refresh_session(self):
        """Rebuild the HTTP session while preserving durable auth/config state."""
        self.session.close()
        self.session = requests.Session()
        self._setup_session()
    
    def _load_rate_limits(self) -> Dict[str, Dict]:
        """Load rate limit state from disk or initialize"""
        state_file = self.shared_state_dir / "rate_limits.json"
        loaded: Dict[str, Dict] = {}
        if state_file.exists():
            try:
                with open(state_file) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    loaded = data
            except:
                pass
        
        # Initialize from config or defaults
        limits = {}
        config_limits = self.config.get("rate_limits", {})
        for endpoint, default in self.DEFAULT_LIMITS.items():
            config_data = config_limits.get(endpoint, default)
            existing = loaded.get(endpoint, {}) if isinstance(loaded.get(endpoint), dict) else {}
            limit = config_data.get("limit", default["limit"])
            window_seconds = config_data.get("window_seconds", default["window_seconds"])
            limits[endpoint] = {
                "remaining": existing.get("remaining", limit),
                "reset": existing.get("reset", int(time.time()) + window_seconds),
                "limit": existing.get("limit", limit),
            }
        return limits
    
    def _save_rate_limits(self):
        """Persist rate limit state to disk"""
        state_file = self.shared_state_dir / "rate_limits.json"
        with open(state_file, 'w') as f:
            json.dump(self.rate_limits, f, indent=2)
    
    def _load_endpoint_health(self) -> Dict[str, str]:
        """Load endpoint health status"""
        state_file = self.state_dir / "endpoint_health.json"
        if state_file.exists():
            try:
                with open(state_file) as f:
                    return json.load(f)
            except:
                pass
        return {endpoint: EndpointHealth.HEALTHY for endpoint in self.DEFAULT_LIMITS.keys()}
    
    def _save_endpoint_health(self):
        """Persist endpoint health status"""
        state_file = self.state_dir / "endpoint_health.json"
        with open(state_file, 'w') as f:
            json.dump(self.endpoint_health, f, indent=2)
    
    def _load_query_ids(self) -> Dict[str, str]:
        """Load query IDs from config"""
        api_config = self.config.get("api_config", {})
        return {
            "UserByScreenName": api_config.get("user_by_screen_name_query_id", "sLVLhk0bGj3MVFEKTdax1w"),
            "UserTweets": api_config.get("user_tweets_query_id", "pQHADmT91zIY83UbK0x4Lw"),
            "UserTweetsAndReplies": api_config.get("user_tweets_and_replies_query_id", "6eh3huj6fJnA3Naupj4w0Q"),
            "TweetDetail": api_config.get("tweet_detail_query_id", ""),
            "SearchTimeline": api_config.get("search_timeline_query_id", ""),
        }

    def refresh_config_and_query_ids(self):
        """Reload config + query IDs at runtime (helps after manual config updates)."""
        self.config = self._load_config()
        self.query_ids = self._load_query_ids()

        # If query IDs were updated in config, allow retries again.
        for endpoint in self.query_ids.keys():
            if self.endpoint_health.get(endpoint) == EndpointHealth.STALE_QUERY_ID:
                self.endpoint_health[endpoint] = EndpointHealth.HEALTHY
        self._save_endpoint_health()
    
    def check_rate_limit(self, endpoint: str, safety_margin: float = 0.9) -> Tuple[bool, Optional[int]]:
        """
        Check if we have budget for this endpoint
        
        Returns:
            (can_proceed, seconds_until_reset)
        """
        if endpoint not in self.rate_limits:
            return True, None
        
        limit_data = self.rate_limits[endpoint]
        now = int(time.time())
        
        # Check if reset time has passed
        if now >= limit_data["reset"]:
            # Reset the bucket
            limit_data["remaining"] = limit_data["limit"]
            limit_data["reset"] = now + 900  # 15 minutes
            self._save_rate_limits()
            return True, None
        
        # Check if we have remaining budget
        threshold = int(limit_data["limit"] * safety_margin)
        if limit_data["remaining"] > 0:
            return True, None
        
        # Rate limited
        seconds_until_reset = limit_data["reset"] - now
        return False, seconds_until_reset

    def wait_for_rate_limit(self, endpoint: str, safety_buffer_seconds: Optional[int] = None) -> int:
        """Sleep until a persisted exhausted endpoint bucket resets."""
        if safety_buffer_seconds is None:
            safety_buffer_seconds = int(self.retry_policy().get("rate_limit_safety_buffer_seconds", 5))
        if endpoint not in self.rate_limits:
            return 0
        limit_data = self.rate_limits[endpoint]
        now = int(time.time())
        remaining = int(limit_data.get("remaining", 1) or 0)
        reset = int(limit_data.get("reset", 0) or 0)
        if remaining > 0 or reset <= now:
            return 0
        sleep_for = max(0, reset - now + int(safety_buffer_seconds))
        if sleep_for:
            print(f"⏳ {endpoint} rate bucket exhausted; sleeping {sleep_for}s until reset.")
            time.sleep(sleep_for)
        return sleep_for

    def seconds_until_reset(self, endpoint: str, safety_buffer_seconds: Optional[int] = None) -> int:
        """Return seconds until the persisted endpoint reset time."""
        if safety_buffer_seconds is None:
            safety_buffer_seconds = int(self.retry_policy().get("rate_limit_safety_buffer_seconds", 5))
        limit_data = self.rate_limits.get(endpoint, {})
        now = int(time.time())
        reset = int(limit_data.get("reset", 0) or 0)
        return max(0, reset - now + int(safety_buffer_seconds))

    def rate_limit_sleep_seconds(self, endpoint: str, response_headers: Optional[dict] = None) -> int:
        """Calculate bounded sleep for 429 from persisted state and response headers."""
        policy = self.retry_policy()
        wait = self.seconds_until_reset(
            endpoint,
            safety_buffer_seconds=int(policy.get("rate_limit_safety_buffer_seconds", 5)),
        )
        headers = response_headers or {}
        retry_after = headers.get("retry-after") if hasattr(headers, "get") else None
        if retry_after:
            try:
                wait = max(wait, int(float(retry_after)))
            except (TypeError, ValueError):
                pass
        max_wait = int(policy.get("max_rate_limit_sleep_seconds", 900))
        return max(0, min(wait, max_wait))
    
    def update_rate_limit(self, endpoint: str, response_headers: dict):
        """Update rate limit state from response headers"""
        if endpoint not in self.rate_limits:
            return
        
        try:
            remaining = int(response_headers.get("x-rate-limit-remaining", -1))
            reset = int(response_headers.get("x-rate-limit-reset", 0))
            limit = int(response_headers.get("x-rate-limit-limit", self.rate_limits[endpoint]["limit"]))
            
            if remaining >= 0:
                self.rate_limits[endpoint]["remaining"] = remaining
            if reset > 0:
                self.rate_limits[endpoint]["reset"] = reset
            if limit > 0:
                self.rate_limits[endpoint]["limit"] = limit
            
            self._save_rate_limits()
            
            # Check if we're hitting limits
            if remaining == 0:
                self.endpoint_health[endpoint] = EndpointHealth.RATE_LIMITED
                self._save_endpoint_health()
        except (ValueError, KeyError):
            pass
    
    def perform_get(
        self,
        endpoint: str,
        url: str,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        context: Optional[Union[RequestContext, Dict]] = None,
        username: Optional[str] = None,
        **kwargs
    ) -> requests.Response:
        """
        Perform a resilient GET request and return the raw response.
        Uses adapter-level retries + bounded manual retries for request exceptions
        and 5xx responses.
        """
        extra_headers = kwargs.pop("headers", None)
        kwargs.setdefault("timeout", self.default_timeout)

        last_exception: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                self.wait_for_rate_limit(endpoint)
                self._human_delay("before_request")
                self.request_count += 1
                request_headers = self._build_request_headers(
                    endpoint,
                    context=context,
                    username=username,
                    extra_headers=extra_headers,
                )
                response = self._get(url, headers=request_headers, **kwargs)
                self.last_status_by_endpoint[endpoint] = response.status_code
                self.update_rate_limit(endpoint, response.headers)

                if response.status_code == 200:
                    self.endpoint_health[endpoint] = EndpointHealth.HEALTHY
                    self._save_endpoint_health()
                elif response.status_code == 429:
                    self.endpoint_health[endpoint] = EndpointHealth.RATE_LIMITED
                    self._save_endpoint_health()
                elif response.status_code == 404:
                    self.endpoint_health[endpoint] = (
                        EndpointHealth.CONTEXT_REJECTED
                        if endpoint in {"UserTweetsAndReplies", "SearchTimeline"}
                        else EndpointHealth.STALE_QUERY_ID
                    )
                    self._save_endpoint_health()
                elif 500 <= response.status_code < 600:
                    if attempt < max_retries - 1:
                        wait = retry_delay * (2 ** attempt)
                        print(f"⚠️  {response.status_code} on {endpoint}, retrying in {wait:.1f}s...")
                        time.sleep(wait)
                        continue
                    self.endpoint_health[endpoint] = EndpointHealth.SERVER_ERROR
                    self._save_endpoint_health()

                return response
            except requests.exceptions.RequestException as exc:
                last_exception = exc
                if attempt < max_retries - 1:
                    wait = retry_delay * (2 ** attempt)
                    print(f"⚠️  Request error on {endpoint}: {exc}, retrying in {wait:.1f}s...")
                    time.sleep(wait)
                    continue
                raise

        if last_exception:
            raise last_exception
        raise RuntimeError(f"Request loop exited unexpectedly for endpoint={endpoint}")

    def make_request(
        self,
        endpoint: str,
        url: str,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        context: Optional[Union[RequestContext, Dict]] = None,
        username: Optional[str] = None,
        **kwargs
    ) -> Optional[requests.Response]:
        """
        Backward-compatible request helper returning None on non-successful states.
        
        Args:
            endpoint: Endpoint name for rate limiting
            url: Full URL to request
            max_retries: Maximum retry attempts for 5xx errors
            retry_delay: Base delay between retries (exponential backoff)
            **kwargs: Additional arguments for requests.get()
        
        Returns:
            Response object or None if failed
        """
        try:
            response = self.perform_get(
                endpoint=endpoint,
                url=url,
                max_retries=max_retries,
                retry_delay=retry_delay,
                context=context,
                username=username,
                **kwargs,
            )
        except requests.exceptions.RequestException as e:
            print(f"✗ Request failed on {endpoint} after {max_retries} attempts: {e}")
            return None

        if response.status_code == 200:
            return response

        if response.status_code == 404:
            print(f"⚠️  404 on {endpoint} - request context or query ID rejected")
            return None

        if response.status_code == 429:
            retry_after = int(response.headers.get("retry-after", 900))
            print(f"⏳ Rate limited on {endpoint}, retry after {retry_after}s")
            return None

        if 500 <= response.status_code < 600:
            return None

        print(f"⚠️  Unexpected status {response.status_code} on {endpoint}")
        return None
    
    def get_query_id(self, endpoint: str) -> Optional[str]:
        """Get query ID for an endpoint"""
        # Keep synced with config for long-running sessions / manual edits.
        self.refresh_config_and_query_ids()
        return self.query_ids.get(endpoint)
    
    def get_endpoint_health(self, endpoint: str) -> str:
        """Get health status of an endpoint"""
        return self.endpoint_health.get(endpoint, EndpointHealth.UNKNOWN_ERROR)

    def get_last_status(self, endpoint: str) -> Optional[int]:
        """Get the last HTTP status observed for an endpoint."""
        return self.last_status_by_endpoint.get(endpoint)
    
    def get_stats(self) -> dict:
        """Get session statistics"""
        elapsed = time.time() - self.session_start
        return {
            "requests_made": self.request_count,
            "session_duration_seconds": int(elapsed),
            "requests_per_minute": round(self.request_count / (elapsed / 60), 2) if elapsed > 0 else 0,
            "rate_limits": self.rate_limits,
            "endpoint_health": self.endpoint_health,
        }
```

## File: shared/core/fetcher_engine.py
```python
#!/usr/bin/env python3
"""
Phase 2 Fetcher Engine

Implements:
- Human-like session warm-up before account fetching
- Strict sequential endpoint processing per account
- Hard-stop 4xx diagnostics with high-visibility debug output
- Cursor-aware pagination with explicit transitions
- Enhanced observability with rich (fallback to std logging)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from shared.core.api_manager import APIManager
from shared.core.windowing import RollingWindowEvaluator
from shared.data_pipeline.storage_manager import StorageManager
from shared.config.tier_config import get_priority_policy, load_tier_config, ordered_accounts

try:
    import pytz
except ImportError:
    print("ERROR: Missing dependency pytz. Run: pip3 install pytz")
    raise

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
except Exception:  # pragma: no cover - fallback path
    Console = None
    Panel = None
    Table = None


TIMEZONE = "Asia/Tehran"
DEFAULT_HISTORICAL_MAX_PAGES = 15
SEP = "═" * 90


class EngineLogger:
    """Rich-first logger with plain fallback."""

    def __init__(self):
        self.rich_enabled = Console is not None
        self.console = Console() if self.rich_enabled else None

    def info(self, message: str):
        if self.rich_enabled:
            self.console.print(f"[bold cyan][INFO][/bold cyan] {message}")
        else:
            print(f"[INFO] {message}")

    def success(self, message: str):
        if self.rich_enabled:
            self.console.print(f"[bold green][OK][/bold green] {message}")
        else:
            print(f"[OK] {message}")

    def warning(self, message: str):
        if self.rich_enabled:
            self.console.print(f"[bold yellow][WARN][/bold yellow] {message}")
        else:
            print(f"[WARN] {message}")

    def error(self, message: str):
        if self.rich_enabled:
            self.console.print(f"[bold red][ERROR][/bold red] {message}")
        else:
            print(f"[ERROR] {message}")

    def banner(self, title: str, body: str):
        if self.rich_enabled and Panel is not None:
            self.console.print(Panel.fit(body, title=title, border_style="magenta"))
        else:
            print(SEP)
            print(title)
            print(SEP)
            print(body)
            print(SEP)

    def show_startup_config(self, config: Dict[str, Any], account_map: Dict[str, Dict], policies: Dict[int, Dict]):
        api_cfg = config.get("api_config", {})
        if self.rich_enabled and Table is not None:
            table = Table(title="Loaded API / Tier Configuration", show_lines=False)
            table.add_column("Key", style="cyan")
            table.add_column("Value", style="white")
            table.add_row("Config File", "shared/config/config.json") # <--- اصلاح شد
            table.add_row("Accounts (tiered)", str(len(account_map)))
            table.add_row("Priority Policies", str(len(policies)))
            table.add_row("UserByScreenName QueryID", str(api_cfg.get("user_by_screen_name_query_id", ""))[:20] + "...")
            table.add_row("UserTweets QueryID", str(api_cfg.get("user_tweets_query_id", ""))[:20] + "...")
            table.add_row("UserTweetsAndReplies QueryID", str(api_cfg.get("user_tweets_and_replies_query_id", ""))[:20] + "...")
            table.add_row("Timeout (sec)", str(api_cfg.get("default_timeout_seconds", 20)))
            self.console.print(table)
        else:
            self.info(f"Config File: shared/config/config.json")
            self.info(f"Accounts (tiered): {len(account_map)}")
            self.info(f"Priority Policies: {len(policies)}")
            self.info(f"Timeout (sec): {api_cfg.get('default_timeout_seconds', 20)}")

    def pagination(self, account: str, endpoint: str, page: int, cursor: Optional[str]):
        cursor_text = cursor if cursor else "END"
        self.info(f"Account: @{account} | Endpoint: {endpoint} | Page: {page} | Next Cursor: {cursor_text}")


class FetcherEngine:
    """Phase 2 sequential fetcher with strict failure visibility."""

    def __init__(self, config_path: str = "shared/config/config.json", subsystem: str = "historical"):
        self.project_root = Path(__file__).resolve().parents[2]
        raw_subsystem = str(subsystem or "historical").strip().lower()
        self.subsystem = "historical_live" if raw_subsystem in {"historical", "live"} else raw_subsystem
        self.logger = EngineLogger()
        self.api_manager = APIManager(config_path=config_path, state_dir=self.project_root / "data" / self.subsystem / "state")
        self.storage_manager = StorageManager(base_dir=self.project_root, timezone=TIMEZONE, subsystem=self.subsystem)
        self.window_evaluator = RollingWindowEvaluator()

        self.config = self.api_manager.config
        self.tz = pytz.timezone(TIMEZONE)
        self.account_map, self.priority_policies = load_tier_config(self.config)
        self.max_cursor_error_retries = int(
            self.config.get("api_config", {}).get("cursor_error_max_retries", 3)
        )
        self.first_request_warmup_seconds = int(
            self.config.get("api_config", {}).get("first_request_warmup_seconds", 15)
        )
        self.pagination_safety_cap_pages = int(
            self.config.get("api_config", {}).get("pagination_safety_cap_pages", 50)
        )
        self.backoff_schedule_seconds = [15, 30, 60]

        self.logger.show_startup_config(self.config, self.account_map, self.priority_policies)

    @staticmethod
    def _compact_json(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    def _endpoint_payload_config(self, endpoint: str) -> Dict[str, Any]:
        payloads = self.config.get("graphql_endpoint_payloads", {})
        endpoint_payload = payloads.get(endpoint, {})
        return endpoint_payload if isinstance(endpoint_payload, dict) else {}

    def _timeline_features(self, endpoint: Optional[str] = None) -> Dict[str, Any]:
        if endpoint:
            configured = self._endpoint_payload_config(endpoint).get("features")
            if isinstance(configured, dict):
                return dict(configured)
        return {
            "rweb_video_screen_enabled": False,
            "rweb_cashtags_enabled": True,
            "profile_label_improvements_pcf_label_in_post_enabled": True,
            "responsive_web_profile_redirect_enabled": False,
            "rweb_tipjar_consumption_enabled": False,
            "verified_phone_label_enabled": False,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "premium_content_api_read_enabled": False,
            "communities_web_enable_tweet_community_results_fetch": True,
            "c9s_tweet_anatomy_moderator_badge_enabled": True,
            "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
            "responsive_web_grok_analyze_post_followups_enabled": True,
            "rweb_cashtags_composer_attachment_enabled": True,
            "responsive_web_jetfuel_frame": True,
            "responsive_web_grok_share_attachment_enabled": True,
            "responsive_web_grok_annotations_enabled": True,
            "articles_preview_enabled": True,
            "responsive_web_edit_tweet_api_enabled": True,
            "rweb_conversational_replies_downvote_enabled": False,
            "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
            "view_counts_everywhere_api_enabled": True,
            "longform_notetweets_consumption_enabled": True,
            "responsive_web_twitter_article_tweet_consumption_enabled": True,
            "content_disclosure_indicator_enabled": True,
            "content_disclosure_ai_generated_indicator_enabled": True,
            "responsive_web_grok_show_grok_translated_post": True,
            "responsive_web_grok_analysis_button_from_backend": True,
            "post_ctas_fetch_enabled": True,
            "freedom_of_speech_not_reach_fetch_enabled": True,
            "standardized_nudges_misinfo": True,
            "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
            "longform_notetweets_rich_text_read_enabled": True,
            "longform_notetweets_inline_media_enabled": False,
            "responsive_web_grok_image_annotation_enabled": True,
            "responsive_web_grok_imagine_annotation_enabled": True,
            "responsive_web_grok_community_note_auto_translation_is_enabled": True,
            "responsive_web_enhance_cards_enabled": False,
        }

    def _timeline_field_toggles(self, endpoint: str) -> Dict[str, Any]:
        configured = self._endpoint_payload_config(endpoint).get("fieldToggles")
        if isinstance(configured, dict):
            return dict(configured)
        return {"withArticlePlainText": False}

    def _timeline_variables(self, endpoint: str, user_id: str, cursor: Optional[str]) -> Dict[str, Any]:
        variables_config = self._endpoint_payload_config(endpoint).get("variables")
        if isinstance(variables_config, dict):
            template_key = "pagination" if cursor else "initial"
            template = variables_config.get(template_key)
            if isinstance(template, dict):
                variables = dict(template)
                variables["userId"] = user_id
                if cursor:
                    variables["cursor"] = cursor
                else:
                    variables.pop("cursor", None)
                return variables

        variables: Dict[str, Any] = {
            "userId": user_id,
            "count": 20,
            "includePromotedContent": True,
        }
        if endpoint == "UserTweetsAndReplies":
            variables["withCommunity"] = True
            variables["withVoice"] = True
        else:
            variables["withQuickPromoteEligibilityTweetFields"] = True
            variables["withVoice"] = True

        if cursor:
            variables["cursor"] = cursor
        return variables

    def _extract_bottom_cursor(self, payload: Dict[str, Any]) -> Optional[str]:
        instructions = (
            payload.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )

        for inst in instructions:
            if inst.get("type") != "TimelineAddEntries":
                continue
            for entry in inst.get("entries", []):
                entry_id = str(entry.get("entryId", ""))
                if "cursor-bottom" in entry_id:
                    value = entry.get("content", {}).get("value")
                    if value:
                        return str(value)
        return None

    def _extract_timeline_items(self, payload: Dict[str, Any], username: str) -> List[Dict[str, Any]]:
        instructions = (
            payload.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )

        items: List[Dict[str, Any]] = []
        for inst in instructions:
            if inst.get("type") != "TimelineAddEntries":
                continue
            for entry in inst.get("entries", []):
                entry_id = str(entry.get("entryId", ""))
                if not entry_id.startswith("tweet-"):
                    continue
                item_content = entry.get("content", {}).get("itemContent", {})
                tweet_result = item_content.get("tweet_results", {}).get("result", {})
                legacy = tweet_result.get("legacy", {})
                rest_id = tweet_result.get("rest_id")
                if not legacy or not rest_id:
                    continue
                created_at = legacy.get("created_at", "")
                items.append(
                    {
                        "id": str(rest_id),
                        "account": username,
                        "timestamp": created_at,
                        "text": legacy.get("full_text") or legacy.get("text") or "",
                        "url": f"https://x.com/{username}/status/{rest_id}",
                        "likes": legacy.get("favorite_count", 0),
                        "retweets": legacy.get("retweet_count", 0),
                        "replies": legacy.get("reply_count", 0),
                        "quotes": legacy.get("quote_count", 0),
                        "bookmarks": legacy.get("bookmark_count", 0),
                        "views": (tweet_result.get("views", {}) or {}).get("count", 0),
                        "type": "Tweet",
                    }
                )
        return items

    def _log_4xx_and_exit(
        self,
        *,
        account: str,
        endpoint: str,
        response,
        request_url: str,
        request_headers: Dict[str, Any],
        variables: Dict[str, Any],
        cursor: Optional[str],
    ):
        block = {
            "status_code": response.status_code,
            "account": account,
            "endpoint": endpoint,
            "request_url": request_url,
            "cursor": cursor,
            "headers": request_headers,
            "variables": variables,
            "response_text": response.text[:4000],
        }
        body = json.dumps(block, indent=2, ensure_ascii=False)
        self.logger.banner("CRITICAL 4xx ERROR - EXECUTION HALTED", body)
        raise SystemExit(1)

    def _log_4xx_details(
        self,
        *,
        account: str,
        endpoint: str,
        response,
        request_url: str,
        request_headers: Dict[str, Any],
        variables: Dict[str, Any],
        cursor: Optional[str],
        title: str = "CRITICAL 4xx ERROR",
    ) -> None:
        """Non-exiting version for cursor error handling with resume support."""
        block = {
            "status_code": response.status_code,
            "account": account,
            "endpoint": endpoint,
            "request_url": request_url,
            "cursor": cursor,
            "headers": request_headers,
            "variables": variables,
            "response_text": response.text[:4000],
        }
        body = json.dumps(block, indent=2, ensure_ascii=False)
        self.logger.banner(title, body)

    def _build_graphql_url(
        self,
        *,
        endpoint: str,
        query_id: str,
        variables: Dict[str, Any],
        features: Dict[str, Any],
        field_toggles: Optional[Dict[str, Any]] = None,
    ) -> str:
        base_url = f"https://x.com/i/api/graphql/{query_id}/{endpoint}"
        query_params = {
            "variables": self._compact_json(variables),
            "features": self._compact_json(features),
        }
        if field_toggles is not None:
            query_params["fieldToggles"] = self._compact_json(field_toggles)
        return f"{base_url}?{urlencode(query_params, quote_via=quote)}"

    def _get_user_id(self, username: str) -> str:
        cached_user_id = self.storage_manager.get_user_id(username)
        if cached_user_id:
            return cached_user_id

        query_id = self.api_manager.get_query_id("UserByScreenName")
        if not query_id:
            raise RuntimeError("Missing query ID for UserByScreenName")

        endpoint = "UserByScreenName"
        variables = {"screen_name": username, "withSafetyModeUserFields": True}
        features = {
            "hidden_profile_subscriptions_enabled": True,
            "rweb_tipjar_consumption_enabled": True,
        }

        request_url = self._build_graphql_url(
            endpoint=endpoint,
            query_id=query_id,
            variables=variables,
            features=features,
        )
        response = self.api_manager.perform_get(endpoint=endpoint, url=request_url, username=username)

        if response.status_code in {400, 401, 403, 404}:
            self._log_4xx_and_exit(
                account=username,
                endpoint=endpoint,
                response=response,
                request_url=request_url,
                request_headers=dict(response.request.headers),
                variables=variables,
                cursor=None,
            )

        response.raise_for_status()
        payload = response.json()
        user_id = (
            payload.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("rest_id")
        )
        if not user_id:
            raise RuntimeError(f"Could not resolve user id for @{username}")
        self.storage_manager.set_user_id(username, str(user_id))
        return str(user_id)

    def _fetch_endpoint_result(
        self,
        *,
        account: str,
        user_id: str,
        endpoint: str,
        max_pages: Optional[int] = None,
        window_days: Optional[int] = None,
        batch_dir: Optional[Path] = None,
        force_refetch: bool = False,
    ) -> Dict[str, Any]:
        started_at = datetime.utcnow().isoformat() + "Z"
        attempts = 0
        error_samples: List[Dict[str, Any]] = []
        last_http_status: Optional[int] = None
        latest_window_coverage: Optional[Dict[str, Any]] = None

        def make_result(
            *,
            status: str,
            outcome: str,
            reason: str,
            pages: List[Dict[str, Any]],
            last_cursor: Optional[str],
            raw_batch: Path,
        ) -> Dict[str, Any]:
            return {
                "account": account,
                "endpoint": endpoint,
                "status": status,
                "outcome": outcome,
                "reason": reason,
                "pages": pages,
                "pages_fetched": len(pages),
                "raw_batch_path": str(raw_batch),
                "last_cursor": last_cursor,
                "last_http_status": last_http_status,
                "attempts": attempts,
                "error_samples": error_samples[-5:],
                "started_at": started_at,
                "finished_at": datetime.utcnow().isoformat() + "Z",
                "window_coverage": latest_window_coverage,
            }

        def record_http_error(response, cursor_value: Optional[str], attempt_number: int) -> None:
            nonlocal last_http_status
            last_http_status = int(response.status_code)
            sample = {
                "status_code": int(response.status_code),
                "cursor": cursor_value,
                "attempt": attempt_number,
                "response_text": str(response.text or "")[:500],
            }
            error_samples.append(sample)

        def classify_http_failure(status_code: int, has_pages: bool, cursor_value: Optional[str]) -> Tuple[str, str, str]:
            if status_code == 404 and cursor_value and has_pages:
                return "partial", "partial_cursor_404", "Cursor returned 404 after successful pages"
            if status_code == 404:
                return "failed", "failed_initial_404", "Initial page returned 404"
            if status_code in {401, 403}:
                if has_pages:
                    return "partial", "partial_http_error", f"HTTP {status_code} after successful pages"
                return "failed", "failed_initial_auth", f"Initial request returned HTTP {status_code}"
            if status_code == 429:
                if has_pages:
                    return "partial", "partial_rate_limited", "Rate limit persisted after successful pages"
                return "failed", "failed_initial_rate_limit", "Initial request stayed rate-limited"
            if 500 <= status_code < 600:
                if has_pages:
                    return "partial", "partial_http_error", f"HTTP {status_code} after successful pages"
                return "failed", "failed_initial_http_error", f"Initial request returned HTTP {status_code}"
            if has_pages:
                return "partial", "partial_http_error", f"HTTP {status_code} after successful pages"
            return "failed", "failed_initial_http_error", f"Initial request returned HTTP {status_code}"

        def finish_with_state(
            *,
            status: str,
            outcome: str,
            reason: str,
            pages: List[Dict[str, Any]],
            cursor_value: Optional[str],
            raw_batch: Path,
        ) -> Dict[str, Any]:
            state_status = "completed" if status == "completed" else status
            state_cursor = "__END__" if status == "completed" else (cursor_value if cursor_value else "__START__")
            self.storage_manager.update_endpoint_state(
                account,
                endpoint,
                last_cursor=state_cursor,
                status=state_status,
                meta={
                    "outcome": outcome,
                    "reason": reason,
                    "last_http_status": last_http_status,
                    "pages_fetched": len(pages),
                    "raw_batch_path": str(raw_batch),
                    "finished_at": datetime.utcnow().isoformat() + "Z",
                },
            )
            return make_result(
                status=status,
                outcome=outcome,
                reason=reason,
                pages=pages,
                last_cursor=state_cursor,
                raw_batch=raw_batch,
            )

        query_id = self.api_manager.get_query_id(endpoint)
        if not query_id:
            raise RuntimeError(f"Missing query ID for endpoint: {endpoint}")
        safety_cap = max(1, int(max_pages or self.pagination_safety_cap_pages))

        features = self._timeline_features(endpoint)
        field_toggles = self._timeline_field_toggles(endpoint)
        existing_state = self.storage_manager.get_endpoint_state(account, endpoint)
        status_value = str(existing_state.get("status", "pending"))
        resume_cursor = existing_state.get("last_cursor")
        raw_batch_path = existing_state.get("raw_batch_path")
        if force_refetch:
            batch_dir = self.storage_manager.create_raw_batch_dir(endpoint, account)
            existing_pages = []
            cursor = None
            status_value = "pending"
        elif batch_dir is None:
            if raw_batch_path and Path(str(raw_batch_path)).exists():
                batch_dir = Path(str(raw_batch_path))
            else:
                batch_dir = self.storage_manager.create_raw_batch_dir(endpoint, account)

        existing_pages = [] if force_refetch else self.storage_manager.load_raw_pages_from_batch(batch_dir)
        cursor: Optional[str] = None if force_refetch else (
            str(resume_cursor)
            if (
                resume_cursor
                and status_value in {"running", "paused", "failed"}
                and str(resume_cursor) not in {"__START__", "__END__"}
            )
            else None
        )
        if cursor:
            self.logger.warning(
                f"Resuming @{account} {endpoint} from saved cursor: {cursor}"
            )
        if status_value == "completed" and existing_pages and window_days:
            coverage = self.window_evaluator.evaluate_raw_pages(existing_pages, account, endpoint, window_days)
            if coverage.complete:
                self.logger.info(f"Skipping @{account} {endpoint}; rolling window already complete at {batch_dir}")
                return make_result(
                    status="completed",
                    outcome="skipped_existing_window_complete",
                    reason=f"Existing raw batch covers rolling window: {coverage.reason}",
                    pages=existing_pages,
                    last_cursor=str(existing_state.get("last_cursor") or "__END__"),
                    raw_batch=batch_dir,
                )

        self.api_manager.warmup_navigation_context(username=account, endpoint=endpoint)
        if self.first_request_warmup_seconds > 0 and not existing_pages:
            self.logger.info(
                f"Mandatory first-request warm-up for @{account} {endpoint}: "
                f"{self.first_request_warmup_seconds}s"
            )
            time.sleep(self.first_request_warmup_seconds)

        # Mark active run state at loop start.
        self.storage_manager.update_endpoint_state(
            account,
            endpoint,
            last_cursor=cursor if cursor else None,
            status="running",
            meta={"raw_batch_path": str(batch_dir)},
        )

        page = len(existing_pages) + 1
        all_items: List[Dict[str, Any]] = list(existing_pages)

        policy = self.api_manager.retry_policy()

        while page <= safety_cap:
            variables = self._timeline_variables(endpoint, user_id, cursor)

            request_url = self._build_graphql_url(
                endpoint=endpoint,
                query_id=query_id,
                variables=variables,
                features=features,
                field_toggles=field_toggles,
            )

            request_headers: Dict[str, str] = {}
            if endpoint == "UserTweetsAndReplies":
                request_headers = {
                    "referer": f"https://x.com/{account}/with_replies",
                    "x-twitter-active-user": "yes",
                }
            elif endpoint == "UserTweets":
                request_headers = {
                    "referer": f"https://x.com/{account}",
                    "x-twitter-active-user": "yes",
                }

            response = None
            page_request_succeeded = False
            max_attempts = max(
                int(policy.get("client_error_attempts", self.max_cursor_error_retries)),
                int(policy.get("server_error_attempts", self.max_cursor_error_retries)),
                int(policy.get("request_error_attempts", self.max_cursor_error_retries)),
            )

            for attempt in range(max_attempts):
                attempts += 1
                try:
                    response = self.api_manager.perform_get(
                        endpoint=endpoint,
                        url=request_url,
                        max_retries=1,
                        username=account,
                        headers=request_headers,
                    )
                    last_http_status = int(response.status_code)

                    if response.status_code == 429:
                        record_http_error(response, cursor, attempt + 1)
                        self._log_4xx_details(
                            account=account,
                            endpoint=endpoint,
                            response=response,
                            request_url=request_url,
                            request_headers=dict(response.request.headers),
                            variables=variables,
                            cursor=cursor,
                            title="RATE LIMITED (SLEEPING AND RETRYING)",
                        )
                        wait = self.api_manager.rate_limit_sleep_seconds(endpoint, response.headers)
                        if wait <= 0:
                            wait = int(policy.get("rate_limit_safety_buffer_seconds", 5))
                        if attempt >= max_attempts - 1:
                            status, outcome, reason = classify_http_failure(429, bool(all_items), cursor)
                            return finish_with_state(
                                status=status,
                                outcome=outcome,
                                reason=reason,
                                pages=all_items,
                                cursor_value=cursor,
                                raw_batch=batch_dir,
                            )
                        self.logger.warning(
                            f"@{account} {endpoint} hit HTTP 429; retrying same page/cursor after {wait}s"
                        )
                        time.sleep(wait)
                        continue

                    if response.status_code in {400, 401, 403, 404}:
                        record_http_error(response, cursor, attempt + 1)
                        client_attempts = int(policy.get("client_error_attempts", self.max_cursor_error_retries))
                        self._log_4xx_details(
                            account=account,
                            endpoint=endpoint,
                            response=response,
                            request_url=request_url,
                            request_headers=dict(response.request.headers),
                            variables=variables,
                            cursor=cursor,
                            title=(
                                "CURSOR ERROR (RETRYING)"
                                if attempt < client_attempts - 1
                                else "CURSOR ERROR (MAX RETRIES REACHED)"
                            ),
                        )
                        if attempt < client_attempts - 1:
                            wait = self.api_manager.jitter_sleep(
                                float(policy.get("client_error_min_seconds", 10)),
                                float(policy.get("client_error_max_seconds", 20)),
                                reason=f"@{account} {endpoint} HTTP {response.status_code} retry {attempt + 1}/{client_attempts}",
                            )
                            self.logger.warning(
                                f"@{account} {endpoint} got HTTP {response.status_code}; retried after {wait:.1f}s "
                                f"(attempt {attempt + 1}/{client_attempts})"
                            )
                            continue

                        status, outcome, reason = classify_http_failure(
                            int(response.status_code), bool(all_items), cursor
                        )
                        self.logger.warning(
                            f"@{account} {endpoint} classified as {outcome}; moving to next account/endpoint."
                        )
                        return finish_with_state(
                            status=status,
                            outcome=outcome,
                            reason=reason,
                            pages=all_items,
                            cursor_value=cursor,
                            raw_batch=batch_dir,
                        )

                    if 500 <= response.status_code < 600:
                        record_http_error(response, cursor, attempt + 1)
                        server_attempts = int(policy.get("server_error_attempts", self.max_cursor_error_retries))
                        if attempt < server_attempts - 1:
                            base = float(policy.get("server_error_base_seconds", 5))
                            max_sleep = float(policy.get("server_error_max_seconds", 60))
                            wait = min(max_sleep, base * (2 ** attempt))
                            self.api_manager.jitter_sleep(wait, wait + base, reason=f"@{account} {endpoint} HTTP {response.status_code}")
                            continue
                        status, outcome, reason = classify_http_failure(int(response.status_code), bool(all_items), cursor)
                        return finish_with_state(
                            status=status,
                            outcome=outcome,
                            reason=reason,
                            pages=all_items,
                            cursor_value=cursor,
                            raw_batch=batch_dir,
                        )

                    response.raise_for_status()
                    page_request_succeeded = True
                    break
                except Exception as exc:
                    error_samples.append({
                        "cursor": cursor,
                        "attempt": attempt + 1,
                        "exception": str(exc)[:500],
                    })
                    request_attempts = int(policy.get("request_error_attempts", self.max_cursor_error_retries))
                    if attempt < request_attempts - 1:
                        base = float(policy.get("request_error_base_seconds", 5))
                        max_sleep = float(policy.get("request_error_max_seconds", 60))
                        wait = min(max_sleep, base * (2 ** attempt))
                        self.logger.warning(
                            f"@{account} {endpoint} request error: {exc}; retrying "
                            f"(attempt {attempt + 1}/{request_attempts})"
                        )
                        self.api_manager.jitter_sleep(wait, wait + base, reason=f"@{account} {endpoint} request error")
                        continue

                    status = "partial" if all_items else "failed"
                    outcome = "partial_request_error" if all_items else "failed_initial_request_error"
                    self.logger.warning(
                        f"@{account} {endpoint} classified as {outcome}; moving on."
                    )
                    return finish_with_state(
                        status=status,
                        outcome=outcome,
                        reason=str(exc)[:500],
                        pages=all_items,
                        cursor_value=cursor,
                        raw_batch=batch_dir,
                    )

            if not page_request_succeeded:
                status = "partial" if all_items else "failed"
                outcome = "partial_unknown_error" if all_items else "failed_initial_unknown_error"
                return finish_with_state(
                    status=status,
                    outcome=outcome,
                    reason="Request loop ended without a successful response",
                    pages=all_items,
                    cursor_value=cursor,
                    raw_batch=batch_dir,
                )

            if response is None:
                status = "partial" if all_items else "failed"
                outcome = "partial_empty_response" if all_items else "failed_initial_empty_response"
                return finish_with_state(
                    status=status,
                    outcome=outcome,
                    reason="No response object returned",
                    pages=all_items,
                    cursor_value=cursor,
                    raw_batch=batch_dir,
                )

            try:
                payload = response.json()
            except Exception as exc:
                error_samples.append({
                    "cursor": cursor,
                    "page": page,
                    "exception": f"JSON parse error: {str(exc)[:500]}",
                })
                status = "partial" if all_items else "failed"
                outcome = "partial_parse_error" if all_items else "failed_initial_parse_error"
                return finish_with_state(
                    status=status,
                    outcome=outcome,
                    reason=f"Could not parse JSON response: {str(exc)[:500]}",
                    pages=all_items,
                    cursor_value=cursor,
                    raw_batch=batch_dir,
                )

            all_items.append(payload)
            self.storage_manager.save_raw_page(batch_dir, page, payload)
            next_cursor = self._extract_bottom_cursor(payload)
            coverage = (
                self.window_evaluator.evaluate_raw_pages(all_items, account, endpoint, window_days)
                if window_days
                else None
            )
            latest_window_coverage = coverage.__dict__ if coverage else None

            self.storage_manager.update_endpoint_state(
                account,
                endpoint,
                last_cursor=next_cursor if next_cursor else "__END__",
                status="running",
                meta={
                    "last_page_fetched_at": datetime.utcnow().isoformat() + "Z",
                    "last_page_number": page,
                    "raw_batch_path": str(batch_dir),
                    "window_coverage": latest_window_coverage,
                },
            )

            if coverage and coverage.complete:
                self.logger.info(
                    f"@{account} {endpoint} rolling window complete: "
                    f"oldest={coverage.oldest_date} targets={coverage.target_dates}"
                )
                return finish_with_state(
                    status="completed",
                    outcome="success_window_complete",
                    reason=f"Rolling window complete: {coverage.reason}",
                    pages=all_items,
                    cursor_value=next_cursor if next_cursor else "__END__",
                    raw_batch=batch_dir,
                )

            self.logger.pagination(account=account, endpoint=endpoint, page=page, cursor=next_cursor)
            if next_cursor:
                self.logger.info(
                    f"Page {page} fetched -> Cursor found: {next_cursor} -> Requesting Page {page + 1}"
                )
                cursor = next_cursor
                page += 1
                self.api_manager.human_delay("between_pages")
                continue

            self.logger.info(
                f"Account: @{account} | Endpoint: {endpoint} | End of pagination reached"
            )
            return finish_with_state(
                status="completed",
                outcome="success_true_end",
                reason="End of pagination reached without cursor",
                pages=all_items,
                cursor_value="__END__",
                raw_batch=batch_dir,
            )

        if page > safety_cap:
            return finish_with_state(
                status="partial",
                outcome="partial_safety_cap_reached",
                reason="Emergency safety page cap reached before rolling window completed",
                pages=all_items,
                cursor_value=cursor if cursor else "__END__",
                raw_batch=batch_dir,
            )

        return finish_with_state(
            status="completed",
            outcome="success_true_end",
            reason="Endpoint fetch completed",
            pages=all_items,
            cursor_value="__END__",
            raw_batch=batch_dir,
        )

    def _fetch_endpoint_pages(
        self,
        *,
        account: str,
        user_id: str,
        endpoint: str,
        max_pages: Optional[int] = None,
        window_days: Optional[int] = None,
        batch_dir: Optional[Path] = None,
    ) -> List[Dict[str, Any]]:
        return self._fetch_endpoint_result(
            account=account,
            user_id=user_id,
            endpoint=endpoint,
            max_pages=max_pages,
            window_days=window_days,
            batch_dir=batch_dir,
        ).get("pages", [])

    def _persist_endpoint_output(self, account: str, endpoint: str, tweets: List[Dict[str, Any]]):
        endpoint_map = {
            "UserTweets": self.storage_manager.user_tweets_dir,
            "UserTweetsAndReplies": self.storage_manager.user_replies_dir,
        }
        target_dir = endpoint_map[endpoint]

        by_date: Dict[str, List[Dict[str, Any]]] = {}
        extracted: List[Dict[str, Any]] = []
        for payload in tweets:
            extracted.extend(self._extract_timeline_items(payload, account))

        for tweet in extracted:
            created = tweet.get("timestamp") or ""
            date_str = None
            if created:
                try:
                    dt = datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y")
                    tehran_dt = dt.astimezone(self.tz)
                    date_str = tehran_dt.strftime("%Y-%m-%d")
                except Exception:
                    date_str = None
            if not date_str:
                date_str = (datetime.now(self.tz) - timedelta(days=0)).strftime("%Y-%m-%d")

            by_date.setdefault(date_str, []).append(tweet)

        for date_str, day_tweets in by_date.items():
            self.storage_manager.save_tweets_to_file(day_tweets, account, date_str, target_dir)

    def run(self, selected_accounts: Optional[List[str]] = None):
        accounts = ordered_accounts(self.account_map) if selected_accounts is None else selected_accounts
        if not accounts:
            self.logger.warning("No accounts found in tier configuration.")
            return

        self.logger.info(f"Starting sequential fetch for {len(accounts)} account(s)")

        for idx, username in enumerate(accounts, start=1):
            self.storage_manager.ensure_account_state(username)
            policy = get_priority_policy(username, self.account_map, self.priority_policies)
            window_days = int(policy.get("historical_window_days", 1))

            self.logger.banner(
                f"ACCOUNT {idx}/{len(accounts)}",
                f"@{username}\npriority={policy.get('priority')}\nwindow_days={window_days}",
            )

            self.logger.info(f"Warm-up session flow for @{username}")
            self.api_manager.warmup_session(username)

            user_id = self._get_user_id(username)
            self.logger.success(f"Resolved @{username} -> user_id={user_id}")

            self.logger.info(f"Sequential Step A: fetching UserTweets for @{username}")
            tweets_only = self._fetch_endpoint_pages(
                account=username,
                user_id=user_id,
                endpoint="UserTweets",
                max_pages=self.pagination_safety_cap_pages,
                window_days=window_days,
            )
            self._persist_endpoint_output(username, "UserTweets", tweets_only)
            self.logger.success(f"@{username} UserTweets complete: {len(tweets_only)} item(s)")

            self.logger.info(f"Sequential Step B: fetching UserTweetsAndReplies for @{username}")
            tweets_and_replies = self._fetch_endpoint_pages(
                account=username,
                user_id=user_id,
                endpoint="UserTweetsAndReplies",
                max_pages=self.pagination_safety_cap_pages,
                window_days=window_days,
            )
            self._persist_endpoint_output(username, "UserTweetsAndReplies", tweets_and_replies)
            self.logger.success(
                f"@{username} UserTweetsAndReplies complete: {len(tweets_and_replies)} item(s)"
            )

            self.logger.success(f"Account @{username} fully completed; moving to next account")

        self.logger.success("All accounts completed.")


def main():
    engine = FetcherEngine(config_path="shared/config/config.json")
    engine.run()


if __name__ == "__main__":
    main()
```

## File: shared/core/set_operations.py
```python
#!/usr/bin/env python3
"""
Tweet set extraction and mathematical set operations.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.exporters.text_export_helper import extract_translation_meta

try:
    import jdatetime
except ImportError:
    jdatetime = None

try:
    import pytz
except ImportError:
    pytz = None


def _gregorian_to_jalali(year: int, month: int, day: int) -> tuple[int, int, int]:
    g_days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    j_days_in_month = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]
    gy = year - 1600
    gm = month - 1
    gd = day - 1
    g_day_no = 365 * gy + (gy + 3) // 4 - (gy + 99) // 100 + (gy + 399) // 400
    for idx in range(gm):
        g_day_no += g_days_in_month[idx]
    if gm > 1 and ((gy + 1600) % 4 == 0 and ((gy + 1600) % 100 != 0 or (gy + 1600) % 400 == 0)):
        g_day_no += 1
    g_day_no += gd
    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    jm = 0
    while jm < 11 and j_day_no >= j_days_in_month[jm]:
        j_day_no -= j_days_in_month[jm]
        jm += 1
    return jy, jm + 1, j_day_no + 1


def _format_jalali(dt: datetime) -> str:
    if jdatetime:
        jalali = jdatetime.datetime.fromgregorian(datetime=dt)
        return jalali.strftime("%Y-%m-%d %H:%M:%S") + " Asia/Tehran"
    jy, jm, jd = _gregorian_to_jalali(dt.year, dt.month, dt.day)
    return f"{jy:04d}-{jm:02d}-{jd:02d} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} Asia/Tehran"


class TweetSetProcessor:
    """Parse raw pages and compute A/B/union/intersection/difference sets."""

    def extract_tweets_from_raw(
        self,
        raw_pages: List[Dict[str, Any]],
        username: Optional[str] = None,
        source_endpoint: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Extract tweets from GraphQL timeline instructions.
        Returns dict[tweet_key] = tweet_object for inherent deduplication.
        """
        result: Dict[str, Dict[str, Any]] = {}
        if not isinstance(raw_pages, list):
            return result

        for page in raw_pages:
            instructions = (
                page.get("data", {})
                .get("user", {})
                .get("result", {})
                .get("timeline", {})
                .get("timeline", {})
                .get("instructions", [])
            )
            if not isinstance(instructions, list):
                continue

            for inst in instructions:
                if not isinstance(inst, dict):
                    continue
                if inst.get("type") != "TimelineAddEntries":
                    continue
                entries = inst.get("entries", [])
                if not isinstance(entries, list):
                    continue

                for entry in entries:
                    if not isinstance(entry, dict):
                        continue

                    tweet_candidate = self._extract_tweet_from_entry(entry)
                    if not tweet_candidate:
                        continue

                    tweet_candidate = self._normalize_tweet(tweet_candidate, username=username, source_endpoint=source_endpoint)
                    key = self._tweet_key(tweet_candidate)
                    if key:
                        result[key] = tweet_candidate

                    content = entry.get("content", {})
                    if isinstance(content, dict):
                        for module_item in content.get("items", []) if isinstance(content.get("items"), list) else []:
                            if not isinstance(module_item, dict):
                                continue
                            item = module_item.get("item", {})
                            if not isinstance(item, dict):
                                continue
                            module_tweet = self._extract_tweet_from_item(item)
                            if not module_tweet:
                                continue
                            module_tweet = self._normalize_tweet(module_tweet, username=username, source_endpoint=source_endpoint)
                            module_key = self._tweet_key(module_tweet)
                            if module_key:
                                result[module_key] = module_tweet

        return result

    def _extract_tweet_from_entry(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        content = entry.get("content", {})
        if not isinstance(content, dict):
            return None
        item_content = content.get("itemContent", {})
        if not isinstance(item_content, dict):
            return None
        return self._extract_tweet_from_item(item_content)

    def _extract_tweet_from_item(self, item_content: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        tweet_results = item_content.get("tweet_results", {})
        if not isinstance(tweet_results, dict):
            return None
        tweet_obj = tweet_results.get("result")
        if not isinstance(tweet_obj, dict):
            return None
        tweet_obj = self._unwrap_tweet_result(tweet_obj)

        legacy = tweet_obj.get("legacy", {})
        if not isinstance(legacy, dict):
            return None
        if not tweet_obj.get("rest_id"):
            return None
        return tweet_obj

    @staticmethod
    def _unwrap_tweet_result(tweet_obj: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(tweet_obj.get("tweet"), dict):
            return tweet_obj["tweet"]
        return tweet_obj

    @staticmethod
    def _tweet_key(tweet_obj: Dict[str, Any]) -> Optional[str]:
        tweet_id = tweet_obj.get("rest_id") or tweet_obj.get("id")
        if not tweet_id:
            return None
        author_id = tweet_obj.get("author_id")
        if not author_id:
            author_id = (
                tweet_obj.get("core", {})
                .get("user_results", {})
                .get("result", {})
                .get("rest_id")
            )
        if author_id:
            return f"{author_id}:{tweet_id}"
        return str(tweet_id)

    def _normalize_tweet(
        self,
        tweet_obj: Dict[str, Any],
        username: Optional[str] = None,
        source_endpoint: Optional[str] = None,
    ) -> Dict[str, Any]:
        legacy = tweet_obj.get("legacy", {}) if isinstance(tweet_obj.get("legacy"), dict) else {}
        core_user = (
            tweet_obj.get("core", {})
            .get("user_results", {})
            .get("result", {})
        )
        user_legacy = core_user.get("legacy", {}) if isinstance(core_user, dict) and isinstance(core_user.get("legacy"), dict) else {}
        author_handle = user_legacy.get("screen_name") or username or "unknown"
        tweet_id = str(tweet_obj.get("rest_id") or "")

        normalized: Dict[str, Any] = {
            "id": tweet_id,
            "rest_id": tweet_id,
            "author_id": core_user.get("rest_id") if isinstance(core_user, dict) else None,
            "account": str(author_handle).lstrip("@"),
            "timestamp": self._format_timestamp(legacy.get("created_at")),
            "created_at": legacy.get("created_at"),
            "raw_timestamp": legacy.get("created_at"),
            "text": legacy.get("full_text") or legacy.get("text") or "",
            "url": f"https://x.com/{str(author_handle).lstrip('@')}/status/{tweet_id}" if tweet_id else "",
            "likes": legacy.get("favorite_count", 0),
            "retweets": legacy.get("retweet_count", 0),
            "replies": legacy.get("reply_count", 0),
            "quotes": legacy.get("quote_count", 0),
            "bookmarks": legacy.get("bookmark_count", 0),
            "views": self._view_count(tweet_obj),
            "entities": self._extract_entities(legacy),
            "source_language": legacy.get("lang"),
            "translation_meta": extract_translation_meta(tweet_obj),
            "conversation_id": legacy.get("conversation_id_str"),
            "in_reply_to_status_id": legacy.get("in_reply_to_status_id_str"),
            "in_reply_to_user_id": legacy.get("in_reply_to_user_id_str"),
            "in_reply_to_screen_name": legacy.get("in_reply_to_screen_name"),
            "type": "Tweet",
        }
        if source_endpoint:
            normalized["source_endpoint"] = source_endpoint

        retweeted = self._nested_tweet(tweet_obj, "retweeted_status_result") or self._nested_tweet(legacy, "retweeted_status_result")
        quoted = self._nested_tweet(tweet_obj, "quoted_status_result")

        if retweeted:
            retweet_legacy = retweeted.get("legacy", {}) if isinstance(retweeted.get("legacy"), dict) else {}
            retweet_user = (
                retweeted.get("core", {})
                .get("user_results", {})
                .get("result", {})
            )
            retweet_user_legacy = retweet_user.get("legacy", {}) if isinstance(retweet_user, dict) and isinstance(retweet_user.get("legacy"), dict) else {}
            normalized.update({
                "type": "Retweet",
                "retweeted_tweet_id": str(retweeted.get("rest_id") or ""),
                "retweeted_author": retweet_user_legacy.get("screen_name"),
                "retweeted_text": retweet_legacy.get("full_text") or retweet_legacy.get("text") or "",
                "retweeted_timestamp": self._format_timestamp(retweet_legacy.get("created_at")),
                "retweeted_translation_meta": extract_translation_meta(retweeted),
            })
        elif str(normalized.get("text") or "").startswith("RT @"):
            normalized.update({
                "type": "Retweet",
                "retweeted_tweet_id": None,
                "retweeted_author": None,
                "retweeted_text": normalized.get("text", ""),
                "retweeted_timestamp": "",
                "retweeted_translation_meta": None,
            })
        elif quoted:
            quoted_legacy = quoted.get("legacy", {}) if isinstance(quoted.get("legacy"), dict) else {}
            quoted_user = (
                quoted.get("core", {})
                .get("user_results", {})
                .get("result", {})
            )
            quoted_user_legacy = quoted_user.get("legacy", {}) if isinstance(quoted_user, dict) and isinstance(quoted_user.get("legacy"), dict) else {}
            normalized.update({
                "type": "Quote",
                "quoted_tweet_id": str(quoted.get("rest_id") or ""),
                "quoted_author": quoted_user_legacy.get("screen_name"),
                "quoted_text": quoted_legacy.get("full_text") or quoted_legacy.get("text") or "",
                "quoted_timestamp": self._format_timestamp(quoted_legacy.get("created_at")),
                "quoted_translation_meta": extract_translation_meta(quoted),
            })
        elif legacy.get("quoted_status_id_str"):
            normalized.update({
                "type": "Quote",
                "quoted_tweet_id": legacy.get("quoted_status_id_str"),
                "quoted_author": None,
                "quoted_text": "",
                "quoted_timestamp": "",
                "quoted_translation_meta": None,
            })
        elif normalized.get("in_reply_to_status_id"):
            normalized["type"] = "Reply"

        return normalized

    @staticmethod
    def _view_count(tweet_obj: Dict[str, Any]) -> int:
        views = tweet_obj.get("views", {}) if isinstance(tweet_obj, dict) else {}
        raw = views.get("count", 0) if isinstance(views, dict) else 0
        try:
            return int(str(raw).replace(",", ""))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _extract_entities(legacy: Dict[str, Any]) -> Dict[str, Any]:
        entities = legacy.get("entities", {}) if isinstance(legacy.get("entities"), dict) else {}
        extended = legacy.get("extended_entities", {}) if isinstance(legacy.get("extended_entities"), dict) else {}
        media = extended.get("media", entities.get("media", []))
        return {
            "urls": [
                {
                    "short": item.get("url"),
                    "expanded": item.get("expanded_url") or item.get("display_url"),
                }
                for item in entities.get("urls", [])
                if isinstance(item, dict)
            ],
            "hashtags": [item.get("text") for item in entities.get("hashtags", []) if isinstance(item, dict) and item.get("text")],
            "mentions": [
                {"handle": item.get("screen_name"), "name": item.get("name")}
                for item in entities.get("user_mentions", [])
                if isinstance(item, dict)
            ],
            "media_links": [item.get("media_url_https") or item.get("expanded_url") for item in media if isinstance(item, dict) and (item.get("media_url_https") or item.get("expanded_url"))],
            "media_types": [item.get("type") for item in media if isinstance(item, dict) and item.get("type")],
        }

    @classmethod
    def _nested_tweet(cls, tweet_obj: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
        nested = tweet_obj.get(key, {})
        if not isinstance(nested, dict):
            return None
        result = nested.get("result")
        if isinstance(result, dict):
            return cls._unwrap_tweet_result(result)
        return None

    @staticmethod
    def _format_timestamp(created_at: Any) -> str:
        raw = str(created_at or "").strip()
        if not raw:
            return "UNKNOWN"
        try:
            dt = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
            if pytz:
                dt = dt.astimezone(pytz.timezone("Asia/Tehran"))
            return _format_jalali(dt)
        except Exception:
            return raw

    def get_union(self, set_a: Dict[str, Dict[str, Any]], set_b: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged = dict(set_a or {})
        merged.update(set_b or {})
        return list(merged.values())

    def get_intersection(self, set_a: Dict[str, Dict[str, Any]], set_b: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        keys = set((set_a or {}).keys()) & set((set_b or {}).keys())
        return [set_b[k] if k in set_b else set_a[k] for k in keys]

    def get_difference_b_minus_a(self, set_a: Dict[str, Dict[str, Any]], set_b: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        keys = set((set_b or {}).keys()) - set((set_a or {}).keys())
        return [set_b[k] for k in keys if k in set_b]
```

## File: shared/core/windowing.py
```python
#!/usr/bin/env python3
"""
Rolling-window coverage helpers for v4 subsystems.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set

from shared.core.set_operations import TweetSetProcessor
from shared.data_pipeline.storage_manager import _format_jalali

try:
    import pytz
except ImportError:  # pragma: no cover
    pytz = None


TIMEZONE = "Asia/Tehran"


@dataclass(frozen=True)
class WindowCoverage:
    complete: bool
    target_dates: List[str]
    covered_dates: List[str]
    missing_dates: List[str]
    oldest_date: Optional[str]
    newest_date: Optional[str]
    crossed_window_start: bool
    item_count: int
    reason: str


def tehran_now() -> datetime:
    if pytz:
        return datetime.now(pytz.timezone(TIMEZONE))
    return datetime.utcnow()


def jalali_date(dt: datetime) -> str:
    if pytz and dt.tzinfo is None:
        dt = pytz.utc.localize(dt).astimezone(pytz.timezone(TIMEZONE))
    elif pytz:
        dt = dt.astimezone(pytz.timezone(TIMEZONE))
    return _format_jalali(dt, "%Y-%m-%d")


def target_jalali_dates(days: int, now_dt: Optional[datetime] = None) -> List[str]:
    current = now_dt or tehran_now()
    return [jalali_date(current - timedelta(days=offset)) for offset in range(max(1, int(days)))]


def parse_twitter_timestamp(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
        if pytz:
            return parsed.astimezone(pytz.timezone(TIMEZONE))
        return parsed
    except Exception:
        return None


def tweet_jalali_date(tweet: Dict[str, Any]) -> Optional[str]:
    for key in ("raw_timestamp", "created_at"):
        parsed = parse_twitter_timestamp(tweet.get(key) if isinstance(tweet, dict) else None)
        if parsed:
            return jalali_date(parsed)
    timestamp = str(tweet.get("timestamp") or "" if isinstance(tweet, dict) else "")
    if len(timestamp) >= 10 and timestamp[:4].isdigit():
        return timestamp[:10]
    return None


def tweet_is_older_than_jalali(tweet: Dict[str, Any], cutoff_jalali: str) -> bool:
    date_value = tweet_jalali_date(tweet)
    return bool(date_value and date_value < cutoff_jalali)


class RollingWindowEvaluator:
    """Evaluate whether raw pages prove a Tehran/Jalali rolling window."""

    def __init__(self):
        self.processor = TweetSetProcessor()

    def extract_endpoint_tweets(
        self,
        raw_pages: List[Dict[str, Any]],
        username: str,
        endpoint: str,
    ) -> List[Dict[str, Any]]:
        extracted = self.processor.extract_tweets_from_raw(raw_pages, username=username, source_endpoint=endpoint)
        return list(extracted.values())

    def evaluate_tweets(
        self,
        tweets: Iterable[Dict[str, Any]],
        window_days: int,
        now_dt: Optional[datetime] = None,
    ) -> WindowCoverage:
        targets = target_jalali_dates(window_days, now_dt=now_dt)
        if not targets:
            targets = target_jalali_dates(1, now_dt=now_dt)
        window_start = targets[-1]
        dates: Set[str] = set()
        item_count = 0
        for tweet in tweets:
            item_count += 1
            date_value = tweet_jalali_date(tweet)
            if date_value:
                dates.add(date_value)

        sorted_dates = sorted(dates)
        oldest = sorted_dates[0] if sorted_dates else None
        newest = sorted_dates[-1] if sorted_dates else None
        crossed = bool(oldest and oldest <= window_start)
        covered = [date for date in targets if date in dates]
        missing = [date for date in targets if date not in dates]

        if not item_count:
            return WindowCoverage(False, targets, covered, missing, oldest, newest, False, item_count, "no_items")
        if not crossed:
            return WindowCoverage(False, targets, covered, missing, oldest, newest, False, item_count, "window_start_not_crossed")
        
        return WindowCoverage(True, targets, covered, missing, oldest, newest, True, item_count, "window_crossed")

    def evaluate_raw_pages(
        self,
        raw_pages: List[Dict[str, Any]],
        username: str,
        endpoint: str,
        window_days: int,
        now_dt: Optional[datetime] = None,
    ) -> WindowCoverage:
        tweets = self.extract_endpoint_tweets(raw_pages, username=username, endpoint=endpoint)
        return self.evaluate_tweets(tweets, window_days=window_days, now_dt=now_dt)
```

## File: shared/data_pipeline/__init__.py
```python
"""Data pipeline package for storage and transformations."""
```

## File: shared/data_pipeline/storage_manager.py
```python
#!/usr/bin/env python3
"""
Storage manager for Phase 3 raw/processed persistence.
"""

from __future__ import annotations

import json
import re
import shutil
import textwrap
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import jdatetime
except ImportError:
    jdatetime = None

try:
    import pytz
except ImportError:
    pytz = None

from shared.exporters.text_export_helper import choose_export_text


def extract_metrics(tweet_obj: Dict[str, Any]) -> Dict[str, Any]:
    """Compatibility helper for legacy callers expecting metric extraction."""
    legacy = tweet_obj.get("legacy", {}) if isinstance(tweet_obj, dict) else {}
    views_obj = tweet_obj.get("views", {}) if isinstance(tweet_obj, dict) else {}
    views_raw = views_obj.get("count", 0) if isinstance(views_obj, dict) else 0
    try:
        views = int(str(views_raw).replace(",", ""))
    except (TypeError, ValueError):
        views = 0
    return {
        "likes": legacy.get("favorite_count", 0),
        "retweets": legacy.get("retweet_count", 0),
        "replies": legacy.get("reply_count", 0),
        "quotes": legacy.get("quote_count", 0),
        "bookmarks": legacy.get("bookmark_count", 0),
        "views": views,
    }


def _gregorian_to_jalali(year: int, month: int, day: int) -> tuple[int, int, int]:
    """Convert Gregorian date to Jalali date; fallback for missing jdatetime."""
    g_days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    j_days_in_month = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]
    gy = year - 1600
    gm = month - 1
    gd = day - 1
    g_day_no = 365 * gy + (gy + 3) // 4 - (gy + 99) // 100 + (gy + 399) // 400
    for idx in range(gm):
        g_day_no += g_days_in_month[idx]
    if gm > 1 and ((gy + 1600) % 4 == 0 and ((gy + 1600) % 100 != 0 or (gy + 1600) % 400 == 0)):
        g_day_no += 1
    g_day_no += gd
    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    jm = 0
    while jm < 11 and j_day_no >= j_days_in_month[jm]:
        j_day_no -= j_days_in_month[jm]
        jm += 1
    return jy, jm + 1, j_day_no + 1


def _format_jalali(dt: datetime, fmt: str) -> str:
    if jdatetime:
        return jdatetime.datetime.fromgregorian(datetime=dt).strftime(fmt)
    jy, jm, jd = _gregorian_to_jalali(dt.year, dt.month, dt.day)
    return (
        fmt.replace("%Y", f"{jy:04d}")
        .replace("%m", f"{jm:02d}")
        .replace("%d", f"{jd:02d}")
        .replace("%H", f"{dt.hour:02d}")
        .replace("%M", f"{dt.minute:02d}")
        .replace("%S", f"{dt.second:02d}")
    )


class StorageManager:
    """Manage raw GraphQL pages and processed tweet set outputs."""

    SET_FOLDER_MAP: Dict[str, str] = {
        "A": "1_user_tweets",
        "B": "2_user_tweets_and_replies",
        "INTERSECTION": "3_intersection",
        "UNION": "4_union",
        "REPLIES_ONLY": "5_replies_only",
        "1_user_tweets": "1_user_tweets",
        "2_user_tweets_and_replies": "2_user_tweets_and_replies",
        "3_intersection": "3_intersection",
        "4_union": "4_union",
        "5_replies_only": "5_replies_only",
        "1_user_tweets (A)": "1_user_tweets",
        "2_user_tweets_and_replies (B)": "2_user_tweets_and_replies",
        "3_intersection (A ∩ B)": "3_intersection",
        "4_union (A ∪ B)": "4_union",
        "5_replies_only (B - A)": "5_replies_only",
    }

    def __init__(
        self,
        project_root: Optional[Path] = None,
        base_dir: Optional[Path] = None,
        timezone: str = "Asia/Tehran",
        subsystem: str = "historical",
    ):
        # اصلاح سطح دسترسی به ریشه پروژه (parents[2])
        self.project_root = project_root or base_dir or Path(__file__).resolve().parents[2]
        self.timezone = timezone
        self.tz = pytz.timezone(timezone) if pytz else None
        
        # نگاشت دقیق به ساختار جدید فولدرها
        raw_sub = str(subsystem or "historical").strip().lower()
        if raw_sub in ["historical", "live"]:
            self.subsystem = "historical_live"
        else:
            self.subsystem = raw_sub  # برای حالت "search"

        self.global_data_root = self.project_root / "data"
        self.data_root = self.global_data_root / self.subsystem
        
        # مسیرهای اصلی بر اساس ساختار جدید
        self.raw_root = self.data_root / "raw"
        self.processed_root = self.data_root / "processed"
        self.state_dir = self.data_root / "state"
        self.reports_dir = self.data_root / "reports"
        self.logs_dir = self.data_root / "logs"

        # حفظ مسیرهای قدیمی (Legacy) برای جلوگیری از خطای وابستگی‌های احتمالی
        self.legacy_data_root = self.global_data_root
        self.legacy_raw_root = self.legacy_data_root / "raw_json"
        self.legacy_processed_root = self.legacy_data_root / "processed"
        self.legacy_reports_dir = self.legacy_data_root / "reports"
        self.global_state_dir = self.global_data_root / "state"
        self.legacy_state_dir = self.legacy_data_root / "STATE"
        self.sync_state_file = self.state_dir / "sync_state.json"
        self.legacy_sync_state_file = self.legacy_state_dir / "sync_state.json"

        # نام‌گذاری دقیق پوشه‌های زیرمجموعه processed و raw
        self.raw_user_tweets_dir = self.raw_root / "UserTweets"
        self.raw_user_replies_dir = self.raw_root / "UserTweetsAndReplies"
        self.user_tweets_dir = self.processed_root / "1_user_tweets"
        self.user_replies_dir = self.processed_root / "2_user_tweets_and_replies"
        self.intersection_dir = self.processed_root / "3_intersection"
        self.merged_dir = self.processed_root / "4_union"
        self.endpoint_diffs_dir = self.processed_root / "5_replies_only"

        self._ensure_base_dirs()

    def _ensure_base_dirs(self) -> None:
        for path in [
            self.raw_user_tweets_dir,
            self.raw_user_replies_dir,
            self.user_tweets_dir,
            self.user_replies_dir,
            self.intersection_dir,
            self.merged_dir,
            self.endpoint_diffs_dir,
            self.state_dir,
            self.reports_dir,
            self.logs_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def create_run_id(self) -> str:
        """Create a Jalali/Tehran timestamped run identifier."""
        return f"run_{_format_jalali(self._tehran_now(), '%Y-%m-%d_%H-%M-%S')}"

    def create_run_report_paths(self, run_id: str) -> Dict[str, Path]:
        safe_run_id = re.sub(r"[^a-zA-Z0-9_\\-]+", "_", str(run_id or self.create_run_id()))
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        return {
            "json": self.reports_dir / f"{safe_run_id}.json",
            "txt": self.reports_dir / f"{safe_run_id}.txt",
        }

    def save_run_report_json(self, report: Dict[str, Any], run_id: str) -> Path:
        paths = self.create_run_report_paths(run_id)
        with paths["json"].open("w", encoding="utf-8") as f:
            json.dump(report if isinstance(report, dict) else {}, f, ensure_ascii=False, indent=2)
        return paths["json"]

    def save_run_report_txt(self, report: Dict[str, Any], run_id: str) -> Path:
        paths = self.create_run_report_paths(run_id)
        summary = report.get("summary", {}) if isinstance(report, dict) else {}
        accounts = report.get("accounts", {}) if isinstance(report, dict) else {}
        lines = [
            f"Run ID: {report.get('run_id', run_id)}",
            f"Started: {report.get('started_at', 'UNKNOWN')}",
            f"Finished: {report.get('finished_at', 'UNKNOWN')}",
            "",
            "Summary",
            f"  Successful endpoints: {summary.get('successful_endpoints', 0)}",
            f"  Partial endpoints: {summary.get('partial_endpoints', 0)}",
            f"  Failed endpoints: {summary.get('failed_endpoints', 0)}",
            f"  Skipped endpoints: {summary.get('skipped_endpoints', 0)}",
            "",
        ]

        for username in sorted(accounts.keys(), key=str.lower):
            account_report = accounts.get(username, {})
            lines.append(f"@{username}")
            if account_report.get("user_id"):
                lines.append(f"  user_id: {account_report['user_id']}")
            if account_report.get("skip_reason"):
                lines.append(f"  skipped: {account_report['skip_reason']}")
            for endpoint, endpoint_report in (account_report.get("endpoints", {}) or {}).items():
                lines.append(
                    "  "
                    f"{endpoint}: status={endpoint_report.get('status')} "
                    f"outcome={endpoint_report.get('outcome')} "
                    f"pages={endpoint_report.get('pages_fetched', 0)} "
                    f"txt_verified={endpoint_report.get('processed_txt_verified', False)}"
                )
                reason = endpoint_report.get("reason")
                if reason:
                    lines.append(f"    reason: {reason}")
            final_sets = account_report.get("final_sets")
            if final_sets:
                lines.append(
                    f"  final_sets: verified={final_sets.get('verified')} "
                    f"counts={final_sets.get('counts', {})}"
                )
            lines.append("")

        with paths["txt"].open("w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
        return paths["txt"]

    # ---------------------------------------------------------------------
    # STATE RECOVERY (cursor persistence)
    # ---------------------------------------------------------------------
    def _read_json_file(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        return {}

    def load_sync_state(self) -> Dict[str, Any]:
        """Load sync state from subsystem path, fallback to legacy paths."""
        data = self._read_json_file(self.sync_state_file)
        if data:
            return data
        global_data = self._read_json_file(self.global_state_dir / "sync_state.json")
        if global_data:
            return global_data
        return self._read_json_file(self.legacy_sync_state_file)

    def save_sync_state(self, state: Dict[str, Any]) -> Path:
        """Persist sync state to subsystem path only."""
        payload = state if isinstance(state, dict) else {}
        self.sync_state_file.parent.mkdir(parents=True, exist_ok=True)
        with self.sync_state_file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return self.sync_state_file

    def get_endpoint_state(self, username: str, endpoint: str) -> Dict[str, Any]:
        """
        Return endpoint sync status for a user.
        Default structure:
          {"last_cursor": None, "status": "pending"}
        """
        uname = self._normalize_username(username)
        state = self.load_sync_state()
        user_state = state.get(uname, {}) if isinstance(state.get(uname, {}), dict) else {}
        ep_state = user_state.get(endpoint, {}) if isinstance(user_state.get(endpoint, {}), dict) else {}
        return {
            "last_cursor": ep_state.get("last_cursor"),
            "status": str(ep_state.get("status", "pending")),
            **ep_state,
        }

    def update_endpoint_state(
        self,
        username: str,
        endpoint: str,
        *,
        last_cursor: Optional[str] = None,
        status: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Path:
        """
        Update one endpoint state atomically inside sync_state.json.
        """
        uname = self._normalize_username(username)
        state = self.load_sync_state()
        if uname not in state or not isinstance(state.get(uname), dict):
            state[uname] = {}
        if endpoint not in state[uname] or not isinstance(state[uname].get(endpoint), dict):
            state[uname][endpoint] = {"last_cursor": None, "status": "pending"}

        if last_cursor is not None:
            state[uname][endpoint]["last_cursor"] = last_cursor
        if status is not None:
            state[uname][endpoint]["status"] = status
        if meta and isinstance(meta, dict):
            state[uname][endpoint].update(meta)

        return self.save_sync_state(state)

    def get_user_id(self, username: str) -> Optional[str]:
        uname = self._normalize_username(username)
        user_state = self.load_sync_state().get(uname, {})
        value = user_state.get("user_id") if isinstance(user_state, dict) else None
        return str(value) if value else None

    def set_user_id(self, username: str, user_id: str) -> Path:
        uname = self._normalize_username(username)
        state = self.load_sync_state()
        if uname not in state or not isinstance(state.get(uname), dict):
            state[uname] = {}
        state[uname]["user_id"] = str(user_id)
        state[uname].pop("skip_current_run", None)
        state[uname].pop("skip_reason", None)
        state[uname].pop("skip_at", None)
        return self.save_sync_state(state)

    def update_account_state(
        self,
        username: str,
        mutator: Callable[[Dict[str, Any]], None],
    ) -> Path:
        """Apply an arbitrary mutation to one account state."""
        uname = self._normalize_username(username)
        state = self.load_sync_state()
        if uname not in state or not isinstance(state.get(uname), dict):
            state[uname] = {}
        mutator(state[uname])
        return self.save_sync_state(state)

    def mark_account_skipped_for_run(self, username: str, reason: str) -> Path:
        """Mark account as skipped for this invocation and both canonical endpoints."""
        now = datetime.utcnow().isoformat() + "Z"

        def mutate(user_state: Dict[str, Any]) -> None:
            user_state["skip_current_run"] = True
            user_state["skip_reason"] = reason
            user_state["skip_at"] = now
            for endpoint in ["UserTweetsAndReplies", "UserTweets"]:
                endpoint_state = user_state.get(endpoint)
                if not isinstance(endpoint_state, dict):
                    endpoint_state = {}
                    user_state[endpoint] = endpoint_state
                endpoint_state.update({
                    "status": "skipped",
                    "skip_reason": reason,
                    "skipped_at": now,
                })

        return self.update_account_state(username, mutate)

    def ensure_account_state(self, username: str) -> Path:
        """Ensure both endpoints exist in sync state for this account."""
        uname = self._normalize_username(username)
        state = self.load_sync_state()
        if uname not in state or not isinstance(state.get(uname), dict):
            state[uname] = {}
        for endpoint in ["UserTweets", "UserTweetsAndReplies"]:
            if endpoint not in state[uname] or not isinstance(state[uname].get(endpoint), dict):
                state[uname][endpoint] = {"last_cursor": None, "status": "pending"}
        return self.save_sync_state(state)

    @staticmethod
    def _normalize_username(username: str) -> str:
        return (username or "unknown").strip().lstrip("@").lower() or "unknown"

    @staticmethod
    def _safe_timestamp(timestamp: str) -> str:
        raw = (timestamp or "").strip()
        if not raw:
            return datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        safe = raw.replace(":", "-").replace(" ", "_").replace("/", "-")
        return safe

    def _tehran_now(self) -> datetime:
        if self.tz:
            return datetime.now(self.tz)
        return datetime.utcnow()

    def _jalali_batch_name(self, dt: Optional[datetime] = None) -> str:
        current = dt or self._tehran_now()
        return _format_jalali(current, "%Y-%m-%d_%H-%M")

    def create_raw_batch_dir(self, endpoint_name: str, username: str, batch_name: Optional[str] = None) -> Path:
        batch = batch_name or self._jalali_batch_name()
        target = self.raw_root / endpoint_name / self._normalize_username(username) / batch
        target.mkdir(parents=True, exist_ok=True)
        return target

    def save_raw_page(self, batch_dir: Path, page_number: int, payload: Dict[str, Any]) -> Path:
        batch_dir.mkdir(parents=True, exist_ok=True)
        output_file = batch_dir / f"page_{int(page_number)}.json"
        with output_file.open("w", encoding="utf-8") as f:
            json.dump(payload if isinstance(payload, dict) else {}, f, ensure_ascii=False, indent=2)
        return output_file

    def load_raw_pages_from_batch(self, batch_path: Any) -> List[Dict[str, Any]]:
        path = Path(str(batch_path)) if batch_path else Path()
        if not path.exists() or not path.is_dir():
            return []
        pages: List[Dict[str, Any]] = []
        for page_file in sorted(path.glob("page_*.json"), key=lambda p: self._page_sort_key(p.name)):
            try:
                with page_file.open("r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, dict):
                    pages.append(payload)
            except Exception:
                continue
        return pages

    def find_raw_batches(self, endpoint_name: str, username: str, include_legacy: bool = True) -> List[Path]:
        """Find subsystem raw batches, optionally including legacy raw_json batches."""
        roots = [self.raw_root / endpoint_name / self._normalize_username(username)]
        if include_legacy:
            roots.append(self.legacy_raw_root / endpoint_name / self._normalize_username(username))
        batches: List[Path] = []
        for root in roots:
            if root.exists():
                batches.extend(path for path in root.iterdir() if path.is_dir())
        return sorted(batches)

    def load_all_raw_pages(self, endpoint_name: str, username: str, include_legacy: bool = True) -> List[Dict[str, Any]]:
        """Load all raw pages for an account/endpoint across known batches."""
        pages: List[Dict[str, Any]] = []
        for batch_dir in self.find_raw_batches(endpoint_name, username, include_legacy=include_legacy):
            pages.extend(self.load_raw_pages_from_batch(batch_dir))
        return pages

    def migrate_legacy_historical_data(self, verify: bool = True) -> Dict[str, Any]:
        """
        Copy legacy v4 data into data/historical and leave legacy files untouched.
        """
        if self.subsystem != "historical_live":
            return {"status": "skipped", "reason": "only_historical_storage_migrates_legacy"}

        mappings = [
            (self.legacy_raw_root, self.raw_root),
            (self.legacy_processed_root, self.processed_root),
            (self.legacy_reports_dir, self.reports_dir),
            (self.global_state_dir / "sync_state.json", self.sync_state_file),
            (self.global_state_dir / "endpoint_health.json", self.state_dir / "endpoint_health.json"),
        ]
        copied = 0
        verified = 0
        for source, target in mappings:
            if not source.exists():
                continue
            if source.is_dir():
                for item in source.rglob("*"):
                    if not item.is_file():
                        continue
                    destination = target / item.relative_to(source)
                    if destination.exists():
                        if not verify or destination.stat().st_size == item.stat().st_size:
                            verified += 1
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, destination)
                    copied += 1
                    if not verify or destination.stat().st_size == item.stat().st_size:
                        verified += 1
            else:
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    copied += 1
                if not verify or target.stat().st_size == source.stat().st_size:
                    verified += 1
        return {"status": "ok", "copied": copied, "verified": verified}

    @staticmethod
    def _page_sort_key(name: str) -> int:
        match = re.search(r"page_(\d+)\.json$", name)
        return int(match.group(1)) if match else 0

    def save_raw_json(self, data: List[Dict[str, Any]], endpoint_name: str, username: str, timestamp: str) -> Path:
        """
        Save unmodified paginated raw JSON pages.
        If destination exists and already contains a JSON array, append pages.
        """
        endpoint_dir = self.raw_root / endpoint_name / self._normalize_username(username)
        endpoint_dir.mkdir(parents=True, exist_ok=True)

        output_file = endpoint_dir / f"{self._safe_timestamp(timestamp)}.json"
        existing_pages: List[Dict[str, Any]] = []

        if output_file.exists():
            try:
                with output_file.open("r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, list):
                    existing_pages = payload
            except Exception:
                existing_pages = []

        merged_pages = existing_pages + (data if isinstance(data, list) else [])
        with output_file.open("w", encoding="utf-8") as f:
            json.dump(merged_pages, f, ensure_ascii=False, indent=2)
        return output_file

    def save_processed_set(self, data_list: List[Dict[str, Any]], set_name: str, username: str) -> Path:
        """Backward-compatible JSON+TXT writer; canonical v4 uses save_processed_txt_set."""
        normalized = str(set_name).strip()
        folder = self.SET_FOLDER_MAP.get(normalized, self.SET_FOLDER_MAP.get(normalized.upper()))
        if not folder:
            raise ValueError(f"Unsupported set_name: {set_name}")

        target_dir = self.processed_root / folder / self._normalize_username(username)
        target_dir.mkdir(parents=True, exist_ok=True)
        output_file = target_dir / f"{folder}.json"

        with output_file.open("w", encoding="utf-8") as f:
            json.dump(data_list if isinstance(data_list, list) else [], f, ensure_ascii=False, indent=2)
        self.save_processed_txt(data_list if isinstance(data_list, list) else [], output_file.with_suffix(".txt"))
        return output_file

    def save_processed_txt_set(self, data_list: List[Dict[str, Any]], set_name: str, username: str) -> List[Path]:
        """Save canonical v4 processed output as JSON plus v3-style dated TXT files."""
        normalized = str(set_name).strip()
        folder = self.SET_FOLDER_MAP.get(normalized, self.SET_FOLDER_MAP.get(normalized.upper()))
        if not folder:
            raise ValueError(f"Unsupported set_name: {set_name}")

        target_dir = self.processed_root / folder / self._normalize_username(username)
        target_dir.mkdir(parents=True, exist_ok=True)
        output_json = target_dir / f"{folder}.json"
        with output_json.open("w", encoding="utf-8") as f:
            json.dump(data_list if isinstance(data_list, list) else [], f, ensure_ascii=False, indent=2)

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for item in data_list if isinstance(data_list, list) else []:
            date_str = self._processed_item_jalali_date(item)
            grouped.setdefault(date_str, []).append(item)

        if not grouped:
            output_file = target_dir / f"{self._jalali_date(self._tehran_now())}.txt"
            self.save_processed_txt([], output_file)
            return [output_file]

        output_files: List[Path] = []
        for date_str in sorted(grouped.keys()):
            output_file = target_dir / f"{date_str}.txt"
            self.save_processed_txt(grouped[date_str], output_file)
            output_files.append(output_file)
        return output_files

    def _processed_item_jalali_date(self, item: Dict[str, Any]) -> str:
        for key in ("created_at", "raw_timestamp"):
            parsed = self._parse_twitter_timestamp(item.get(key) if isinstance(item, dict) else None)
            if parsed:
                return self._jalali_date(parsed)

        timestamp = str(item.get("timestamp") or "" if isinstance(item, dict) else "").strip()
        match = re.search(r"(\d{4}-\d{2}-\d{2})", timestamp)
        if match:
            return match.group(1)

        return self._jalali_date(self._tehran_now())

    def _parse_twitter_timestamp(self, value: Any) -> Optional[datetime]:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
            return parsed.astimezone(self.tz) if self.tz else parsed
        except Exception:
            return None

    def _jalali_date(self, dt: datetime) -> str:
        return _format_jalali(dt, "%Y-%m-%d")

    def save_processed_txt(self, data_list: List[Dict[str, Any]], output_file: Path) -> Path:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with output_file.open("w", encoding="utf-8") as f:
            f.write(self._format_tweets(data_list or []))
        return output_file

    def _format_tweets(self, tweets: List[Dict[str, Any]]) -> str:
        if not tweets:
            return "No tweets to export.\n"
        return "".join(self._format_item(tweet) for tweet in tweets)

    def _format_count(self, value: Any) -> str:
        """Format numeric count values safely"""
        if value is None or value == "unknown":
            return "UNKNOWN"
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return str(value).upper() if str(value).lower() == "unknown" else str(value)

    def _wrap(self, text: str, width: int = 70) -> str:
        """Wrap long text to readable width."""
        lines = []
        for para in str(text).split("\n"):
            if para:
                lines.append(
                    textwrap.fill(
                        para, width=width, break_long_words=False, break_on_hyphens=False
                    )
                )
            else:
                lines.append("")
        return "\n".join(lines)

    def _format_entities(self, tweet: Dict[str, Any]) -> List[str]:
        """Format URLs, hashtags, mentions, and media from parsed entities."""
        lines: List[str] = []

        entities = tweet.get("entities", {}) or {}
        urls = [entry.get("expanded") or entry.get("short") for entry in entities.get("urls", []) if isinstance(entry, dict) and (entry.get("expanded") or entry.get("short"))]
        if not urls:
            text = str(tweet.get("text") or "")
            urls = sorted(set(re.findall(r"https?://\S+", text)))

        if urls:
            lines.append("🔗 Links:")
            for url in urls:
                lines.append(f"   → {url}")
            lines.append("")

        media_links = entities.get("media_links", [])
        if media_links:
            media_types = entities.get("media_types", [])
            media_type_suffix = f" ({', '.join(media_types)})" if media_types else ""
            lines.append(f"📎 Media: {len(media_links)} item(s){media_type_suffix}")
            for media_url in media_links:
                lines.append(f"   → {media_url}")
            lines.append("")

        hashtags = entities.get("hashtags", [])
        if hashtags:
            lines.append("🏷️  " + " ".join(f"#{tag}" for tag in hashtags))
            lines.append("")

        mentions = entities.get("mentions", [])
        if mentions:
            lines.append("👥 Mentions:")
            for mention in mentions:
                if isinstance(mention, dict):
                    handle = mention.get("handle", "")
                    name = mention.get("name", "")
                    lines.append(f"   @{handle} ({name})".strip())
            lines.append("")

        return lines

    def _metric_line(self, source: Dict[str, Any]) -> str:
        return (
            f"💬 Replies: {self._format_count(source.get('replies'))}  "
            f"🔁 Retweets: {self._format_count(source.get('retweets'))}  "
            f"❤️ Likes: {self._format_count(source.get('likes'))}  "
            f"💭 Quotes: {self._format_count(source.get('quotes'))}  "
            f"🔖 Bookmarks: {self._format_count(source.get('bookmarks'))}  "
            f"👁 Views: {self._format_count(source.get('views'))}"
        )

    def _get_type_icon(self, tweet_type: str) -> str:
        """Return icon for tweet type"""
        mapping = {
            "Tweet": "📝",
            "Retweet": "🔁",
            "Reply": "💬",
            "Quote": "💭",
        }
        return mapping.get(tweet_type, "📝")

    def _format_tweet(self, tweet: Dict[str, Any]) -> str:
        text_payload = choose_export_text(tweet.get("text", ""), tweet.get("source_language"), tweet.get("translation_meta"))
        lines = ["💬 TWEET", ""]
        lines.append(f"👤 @{tweet.get('account', 'unknown')}")
        lines.append(f"📅 {tweet.get('timestamp', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._wrap(text_payload["text"]))
        if text_payload.get("note"):
            lines.append("")
            lines.append(text_payload["note"])
        lines.append("")
        lines.extend(self._format_entities(tweet))
        lines.append(f"🔗 {tweet.get('url', '')}")
        lines.append(f"🆔 Tweet ID: {tweet.get('id', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._metric_line(tweet))
        if "source_endpoint" in tweet:
            lines.extend(["", f"Source Endpoint: {tweet['source_endpoint']}"])
        lines.extend(["", "═" * 70, "═" * 70, ""])
        return "\n".join(lines)

    def _format_retweet(self, tweet: Dict[str, Any]) -> str:
        original_export = choose_export_text(tweet.get("retweeted_text") or tweet.get("text", ""), None, tweet.get("retweeted_translation_meta"))
        lines = ["🔁 RETWEET", ""]
        lines.append(f"👤 Retweeted by: @{tweet.get('account', 'unknown')}")
        lines.append(f"📅 Retweeted at: {tweet.get('timestamp', 'UNKNOWN')}")
        lines.append("")
        lines.append("┌" + "─" * 68 + "┐")
        lines.append("│ ORIGINAL TWEET" + " " * 53 + "│")
        lines.append("├" + "─" * 68 + "┤")

        original_author = tweet.get("retweeted_author")
        original_tweet_id = tweet.get("retweeted_tweet_id")
        original_author_display = f"@{original_author}" if original_author else "UNKNOWN"
        author_line = f"👤 {original_author_display}"
        lines.append(f"│ {author_line}" + " " * max(0, 68 - len(f"│ {author_line}")) + "│")
        lines.append("│" + " " * 68 + "│")

        text = self._wrap(original_export["text"])
        for line in text.split("\n"):
            for wrapped in textwrap.wrap(line, width=66) if line else [""]:
                lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
        if original_export.get("note"):
            lines.append("│" + " " * 68 + "│")
            note_text = original_export["note"]
            for wrapped in textwrap.wrap(note_text, width=66):
                lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
        lines.append("│" + " " * 68 + "│")

        if tweet.get("retweeted_timestamp"):
            time_line = f"📅 {tweet['retweeted_timestamp']}"
            lines.append(f"│ {time_line}" + " " * max(0, 68 - len(f"│ {time_line}")) + "│")
            lines.append("│" + " " * 68 + "│")

        if original_tweet_id:
            original_url = f"https://x.com/{original_author or 'i'}/status/{original_tweet_id}"
            link_line = f"🔗 {original_url}"
            id_line = f"🆔 Tweet ID: {original_tweet_id}"
            lines.append(f"│ {link_line}" + " " * max(0, 68 - len(f"│ {link_line}")) + "│")
            lines.append(f"│ {id_line}" + " " * max(0, 68 - len(f"│ {id_line}")) + "│")
        else:
            lines.append("│ 🔗 UNKNOWN" + " " * 57 + "│")
            lines.append("│ 🆔 Tweet ID: UNKNOWN" + " " * 46 + "│")

        engagement_line = self._metric_line(tweet)
        lines.append("│" + " " * 68 + "│")
        lines.append(f"│ {engagement_line}" + " " * max(0, 68 - len(f"│ {engagement_line}")) + "│")
        lines.append("└" + "─" * 68 + "┘")

        if "source_endpoint" in tweet:
            lines.extend(["", f"Source Endpoint: {tweet['source_endpoint']}"])
        lines.extend(["", "═" * 70, "═" * 70, ""])
        return "\n".join(lines)

    def _format_reply(self, tweet: Dict[str, Any]) -> str:
        reply_text_payload = choose_export_text(tweet.get("text", ""), tweet.get("source_language"), tweet.get("translation_meta"))
        lines = ["↩️ REPLY", ""]
        lines.append(f"👤 Reply by: @{tweet.get('account', 'unknown')}")
        lines.append(f"📅 {tweet.get('timestamp', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._wrap(reply_text_payload["text"]))
        if reply_text_payload.get("note"):
            lines.append("")
            lines.append(reply_text_payload["note"])
        lines.append("")
        lines.extend(self._format_entities(tweet))
        lines.append(f"🔗 {tweet.get('url', '')}")
        lines.append(f"🆔 Tweet ID: {tweet.get('id', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._metric_line(tweet))
        lines.append("")

        parent_id = tweet.get("in_reply_to_status_id")
        parent_screen = tweet.get("in_reply_to_screen_name")
        parent_tweet = tweet.get("parent_tweet", {}) or {}
        chain = tweet.get("conversation_chain", []) or []
        if chain:
            lines.append("┌" + "─" * 68 + "┐")
            lines.append("│ CONVERSATION THREAD" + " " * 48 + "│")
            lines.append("├" + "─" * 68 + "┤")
            for idx, node in enumerate(chain, start=1):
                role = "Direct parent" if idx == len(chain) else f"Ancestor {idx}"
                author_line = f"{role}: @{node.get('account', 'unknown')}"
                lines.append(f"│ {author_line}" + " " * max(0, 68 - len(f"│ {author_line}")) + "│")
                if node.get("timestamp"):
                    time_line = f"📅 {node['timestamp']}"
                    lines.append(f"│ {time_line}" + " " * max(0, 68 - len(f"│ {time_line}")) + "│")
                node_text_payload = choose_export_text(
                    node.get("text", ""),
                    node.get("source_language"),
                    node.get("translation_meta"),
                )
                text_value = self._wrap(node_text_payload["text"])
                for line in text_value.split("\n"):
                    for wrapped in textwrap.wrap(line, width=66) if line else [""]:
                        lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
                if node_text_payload.get("note"):
                    lines.append("│" + " " * 68 + "│")
                    for wrapped in textwrap.wrap(node_text_payload["note"], width=66):
                        lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
                if node.get("url"):
                    link_line = f"🔗 {node['url']}"
                    lines.append(f"│ {link_line}" + " " * max(0, 68 - len(f"│ {link_line}")) + "│")
                if node.get("id"):
                    id_line = f"🆔 Tweet ID: {node['id']}"
                    lines.append(f"│ {id_line}" + " " * max(0, 68 - len(f"│ {id_line}")) + "│")
                if idx != len(chain):
                    lines.append("│" + " " * 68 + "│")
                    lines.append("│ replies to" + " " * 58 + "│")
                    lines.append("│" + " " * 68 + "│")
            lines.append("└" + "─" * 68 + "┘")
            lines.append("")

        lines.append("┌" + "─" * 68 + "┐")
        lines.append("│ IN REPLY TO" + " " * 56 + "│")
        lines.append("├" + "─" * 68 + "┤")
        parent_label = f"@{parent_screen}" if parent_screen else "UNKNOWN"
        if parent_tweet:
            parent_label = f"@{parent_tweet.get('account', parent_label).lstrip('@')}"
        label_line = f"👤 {parent_label}"
        lines.append(f"│ {label_line}" + " " * max(0, 68 - len(f"│ {label_line}")) + "│")
        if tweet.get("conversation_id"):
            conv_line = f"🧵 Conversation ID: {tweet['conversation_id']}"
            lines.append(f"│ {conv_line}" + " " * max(0, 68 - len(f"│ {conv_line}")) + "│")
        lines.append("│" + " " * 68 + "│")

        if parent_tweet.get("text"):
            parent_text_payload = choose_export_text(
                parent_tweet.get("text", ""),
                parent_tweet.get("source_language"),
                parent_tweet.get("translation_meta"),
            )
            parent_text = self._wrap(parent_text_payload["text"])
            for line in parent_text.split("\n"):
                for wrapped in textwrap.wrap(line, width=66) if line else [""]:
                    lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
            if parent_text_payload.get("note"):
                lines.append("│" + " " * 68 + "│")
                for wrapped in textwrap.wrap(parent_text_payload["note"], width=66):
                    lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
            lines.append("│" + " " * 68 + "│")

        if parent_id:
            parent_url = parent_tweet.get("url") or f"https://x.com/{parent_screen or 'i'}/status/{parent_id}"
            link_line = f"🔗 {parent_url}"
            id_line = f"🆔 Tweet ID: {parent_id}"
            lines.append(f"│ {link_line}" + " " * max(0, 68 - len(f"│ {link_line}")) + "│")
            lines.append(f"│ {id_line}" + " " * max(0, 68 - len(f"│ {id_line}")) + "│")
        else:
            lines.append("│ 🔗 UNKNOWN" + " " * 57 + "│")
            lines.append("│ 🆔 Tweet ID: UNKNOWN" + " " * 46 + "│")
        lines.append("└" + "─" * 68 + "┘")

        if "source_endpoint" in tweet:
            lines.extend(["", f"Source Endpoint: {tweet['source_endpoint']}"])
        lines.extend(["", "═" * 70, "═" * 70, ""])
        return "\n".join(lines)

    def _format_quote(self, tweet: Dict[str, Any]) -> str:
        quote_text_payload = choose_export_text(tweet.get("text", ""), tweet.get("source_language"), tweet.get("translation_meta"))
        lines = ["📎 QUOTE TWEET", ""]
        lines.append(f"👤 Quote by: @{tweet.get('account', 'unknown')}")
        lines.append(f"📅 {tweet.get('timestamp', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._wrap(quote_text_payload["text"]))
        if quote_text_payload.get("note"):
            lines.append("")
            lines.append(quote_text_payload["note"])
        lines.append("")
        lines.extend(self._format_entities(tweet))
        lines.append(f"🔗 {tweet.get('url', '')}")
        lines.append(f"🆔 Tweet ID: {tweet.get('id', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._metric_line(tweet))
        lines.append("")
        lines.append("┌" + "─" * 68 + "┐")
        lines.append("│ QUOTED TWEET" + " " * 55 + "│")
        lines.append("├" + "─" * 68 + "┤")
        quoted_author = tweet.get("quoted_author")
        quoted_id = tweet.get("quoted_tweet_id")
        if quoted_author:
            author_line = f"👤 @{quoted_author}"
            lines.append(f"│ {author_line}" + " " * max(0, 68 - len(f"│ {author_line}")) + "│")
        else:
            lines.append("│ 👤 UNKNOWN" + " " * 57 + "│")
        lines.append("│" + " " * 68 + "│")

        if tweet.get("quoted_timestamp"):
            time_line = f"📅 {tweet['quoted_timestamp']}"
            lines.append(f"│ {time_line}" + " " * max(0, 68 - len(f"│ {time_line}")) + "│")
            lines.append("│" + " " * 68 + "│")

        if tweet.get("quoted_text"):
            quoted_text_payload = choose_export_text(
                tweet.get("quoted_text", ""),
                None,
                tweet.get("quoted_translation_meta"),
            )
            quoted_text = self._wrap(quoted_text_payload["text"])
            for line in quoted_text.split("\n"):
                for wrapped in textwrap.wrap(line, width=66) if line else [""]:
                    lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
            if quoted_text_payload.get("note"):
                lines.append("│" + " " * 68 + "│")
                for wrapped in textwrap.wrap(quoted_text_payload["note"], width=66):
                    lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
            lines.append("│" + " " * 68 + "│")

        if quoted_id:
            quoted_url = f"https://x.com/{quoted_author or 'i'}/status/{quoted_id}"
            link_line = f"🔗 {quoted_url}"
            id_line = f"🆔 Tweet ID: {quoted_id}"
            lines.append(f"│ {link_line}" + " " * max(0, 68 - len(f"│ {link_line}")) + "│")
            lines.append(f"│ {id_line}" + " " * max(0, 68 - len(f"│ {id_line}")) + "│")
        else:
            lines.append("│ 🔗 UNKNOWN" + " " * 57 + "│")
            lines.append("│ 🆔 Tweet ID: UNKNOWN" + " " * 46 + "│")
        lines.append("└" + "─" * 68 + "┘")
        if "source_endpoint" in tweet:
            lines.extend(["", f"Source Endpoint: {tweet['source_endpoint']}"])
        lines.extend(["", "═" * 70, "═" * 70, ""])
        return "\n".join(lines)

    def _format_item(self, tweet: Dict[str, Any]) -> str:
        tweet_type = str(tweet.get("type") or "Tweet").lower()
        if tweet_type == "retweet":
            return self._format_retweet(tweet)
        if tweet_type == "reply":
            return self._format_reply(tweet)
        if tweet_type == "quote":
            return self._format_quote(tweet)
        return self._format_tweet(tweet)

    def save_tweets_to_file(self, tweets: List[Dict[str, Any]], account: str, date_str: str, output_dir: Path) -> int:
        """
        Compatibility writer for endpoint text snapshots used by fetcher engine.
        """
        account_dir = output_dir / self._normalize_username(account)
        account_dir.mkdir(parents=True, exist_ok=True)
        output_file = account_dir / f"{date_str}.txt"

        rows: List[str] = []
        for tweet in tweets or []:
            text = (
                (tweet.get("legacy", {}) if isinstance(tweet.get("legacy", {}), dict) else {}).get("full_text")
                or tweet.get("text")
                or ""
            )
            tweet_id = tweet.get("rest_id") or tweet.get("id") or "unknown"
            timestamp = (
                (tweet.get("legacy", {}) if isinstance(tweet.get("legacy", {}), dict) else {}).get("created_at")
                or tweet.get("timestamp")
                or "UNKNOWN"
            )
            rows.append(f"[{timestamp}] {tweet_id}\n{text}\n{'-' * 40}\n")

        with output_file.open("w", encoding="utf-8") as f:
            f.write("\n".join(rows) if rows else "No tweets.\n")
        return len(tweets or [])

    def log_event(self, log_type: str, message: str) -> Path:
        """Compatibility event logger used by older modules."""
        safe_name = re.sub(r"[^a-zA-Z0-9_\\-]+", "_", log_type or "events")
        log_file = self.logs_dir / f"{safe_name}.log"
        line = f"{datetime.utcnow().isoformat()}Z | {message}\n"
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line)
        return log_file

    @staticmethod
    def get_jalali_date(dt: Optional[datetime] = None) -> str:
        """Compatibility date helper with Jalali output when available."""
        target = dt or datetime.utcnow()
        return _format_jalali(target, "%Y-%m-%d")

    @staticmethod
    def get_jalali_datetime(dt: Optional[datetime] = None) -> str:
        """Compatibility datetime helper with Jalali output when available."""
        target = dt or datetime.utcnow()
        return _format_jalali(target, "%Y-%m-%d %H:%M:%S")
```

## File: shared/exporters/__init__.py
```python
"""Export helpers for text and structured output formatting."""
```

## File: shared/exporters/text_export_helper.py
```python
#!/usr/bin/env python3
"""
Translation-aware TXT export helpers.

This module is intentionally storage/output oriented. It does not mutate raw
payloads and does not change transport behavior.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


ALLOWED_ORIGINAL_LANGS = {"en", "fa"}


def _normalize_lang(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _normalize_translation_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}
    source_language = _normalize_lang(data.get("source_language"))
    destination_language = _normalize_lang(data.get("destination_language")) or "en"
    full_translation = _clean_text(data.get("translation"))
    preview_translation = _clean_text(data.get("preview_translation"))
    is_available = bool(payload.get("is_available", False))

    return {
        "source_language": source_language,
        "destination_language": destination_language,
        "translation": full_translation,
        "preview_translation": preview_translation,
        "is_available": is_available,
        "has_translation": bool(full_translation or preview_translation),
    }


def extract_translation_meta(raw_obj: Any, scan_limit: int = 500) -> Dict[str, Any]:
    """
    Extract grok translation payload from nested tweet-like objects.

    The payload can appear at different wrapper depths, so this uses a bounded
    graph scan and returns the first valid translation object found.
    """
    stack = [raw_obj]
    scanned = 0

    while stack and scanned < scan_limit:
        node = stack.pop()
        scanned += 1

        if isinstance(node, dict):
            if "grok_translated_post_with_availability" in node:
                payload = node.get("grok_translated_post_with_availability")
                if isinstance(payload, dict):
                    return _normalize_translation_payload(payload)

            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    stack.append(item)

    return {
        "source_language": None,
        "destination_language": "en",
        "translation": "",
        "preview_translation": "",
        "is_available": False,
        "has_translation": False,
    }


def choose_export_text(
    original_text: Any,
    source_language: Optional[str],
    translation_meta: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Choose display text for TXT export:
    - Keep original text for en/fa.
    - Otherwise prefer full translation, then preview translation.
    - If translation missing, keep original + UNKNOWN marker.
    """
    original = _clean_text(original_text)
    src_lang = _normalize_lang(source_language)
    meta = translation_meta if isinstance(translation_meta, dict) else {}
    meta_src_lang = _normalize_lang(meta.get("source_language"))
    effective_src_lang = src_lang or meta_src_lang

    full_translation = _clean_text(meta.get("translation"))
    preview_translation = _clean_text(meta.get("preview_translation"))
    translated_text = full_translation or preview_translation

    if effective_src_lang in ALLOWED_ORIGINAL_LANGS:
        return {"text": original, "note": None, "used_translation": False}

    if translated_text:
        src = effective_src_lang or "unknown"
        return {
            "text": translated_text,
            "note": f"[Translated from {src} -> en]",
            "used_translation": True,
        }

    if effective_src_lang and effective_src_lang not in ALLOWED_ORIGINAL_LANGS:
        return {
            "text": original,
            "note": f"[Translation from {effective_src_lang} -> en : UNKNOWN]",
            "used_translation": False,
        }

    return {"text": original, "note": None, "used_translation": False}
```

## File: shared/tools/check_replies_parity.py
```python
#!/usr/bin/env python3
"""Offline parity check for v4 UserTweetsAndReplies requests vs test_replies_endpoint.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DIAGNOSTIC = PROJECT_ROOT / "test_replies_endpoint.py"
sys.path.insert(0, str(PROJECT_ROOT))


def load_diagnostic():
    spec = importlib.util.spec_from_file_location("test_replies_endpoint", DIAGNOSTIC)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {DIAGNOSTIC}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lower_headers(headers: Dict[str, Any]) -> Dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def query_payload(url: str) -> Dict[str, Any]:
    parsed = parse_qs(urlparse(url).query)
    return {key: json.loads(values[0]) for key, values in parsed.items() if values}


def main() -> None:
    from shared.core.fetcher_engine import FetcherEngine

    diag = load_diagnostic()
    engine = FetcherEngine(config_path="shared/config/config.json")
    query_id = engine.api_manager.get_query_id("UserTweetsAndReplies")
    variables = engine._timeline_variables("UserTweetsAndReplies", diag.USER_ID, None)
    features = engine._timeline_features("UserTweetsAndReplies")
    field_toggles = engine._timeline_field_toggles("UserTweetsAndReplies")
    v4_url = engine._build_graphql_url(
        endpoint="UserTweetsAndReplies",
        query_id=query_id,
        variables=variables,
        features=features,
        field_toggles=field_toggles,
    )
    v4_headers = lower_headers(
        engine.api_manager._build_request_headers(
            "UserTweetsAndReplies",
            username=diag.USERNAME,
            extra_headers={"referer": f"https://x.com/{diag.USERNAME}/with_replies", "x-twitter-active-user": "yes"},
        )
    )
    diag_url = diag.build_url(None)
    diag_headers = lower_headers(diag.build_headers())

    header_keys = [
        "accept",
        "accept-encoding",
        "accept-language",
        "authorization",
        "content-type",
        "cookie",
        "dnt",
        "priority",
        "referer",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "user-agent",
        "x-client-transaction-id",
        "x-csrf-token",
        "x-twitter-active-user",
        "x-twitter-auth-type",
        "x-twitter-client-language",
    ]

    mismatches = []
    if urlparse(v4_url).path != urlparse(diag_url).path:
        mismatches.append(("url.path", urlparse(v4_url).path, urlparse(diag_url).path))
    if query_payload(v4_url) != query_payload(diag_url):
        mismatches.append(("query_payload", query_payload(v4_url), query_payload(diag_url)))
    for key in header_keys:
        if v4_headers.get(key) != diag_headers.get(key):
            mismatches.append((f"header.{key}", v4_headers.get(key), diag_headers.get(key)))

    if mismatches:
        print("UserTweetsAndReplies parity mismatches:")
        for name, v4_value, diag_value in mismatches:
            print(f"- {name}\n  v4:   {v4_value}\n  diag: {diag_value}")
        raise SystemExit(1)

    print("UserTweetsAndReplies request parity: OK")


if __name__ == "__main__":
    main()
```

## File: shared/tools/diagnose_replies_only.py
```python
#!/usr/bin/env python3
"""
Diagnose why UserTweetsAndReplies minus UserTweets is empty for an account.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.core.set_operations import TweetSetProcessor
from shared.data_pipeline.storage_manager import StorageManager


def key_by_id(tweet: Dict[str, Any]) -> str:
    return str(tweet.get("id") or tweet.get("rest_id") or "")


def describe_set(name: str, tweets: Dict[str, Dict[str, Any]]) -> None:
    type_counts = Counter(tweet.get("type") for tweet in tweets.values())
    reply_flags = sum(1 for tweet in tweets.values() if tweet.get("in_reply_to_status_id"))
    accounts = Counter(str(tweet.get("account", "unknown")).lower() for tweet in tweets.values())
    print(
        f"{name}: count={len(tweets)} "
        f"types={dict(type_counts)} reply_flags={reply_flags} "
        f"top_accounts={accounts.most_common(5)}"
    )


def load_pages(storage: StorageManager, username: str, endpoint: str) -> list[dict]:
    state = storage.get_endpoint_state(username, endpoint)
    batch = state.get("raw_batch_path")
    if batch:
        pages = storage.load_raw_pages_from_batch(batch)
        if pages:
            return pages
    return storage.load_all_raw_pages(endpoint, username, include_legacy=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose UserTweetsAndReplies minus UserTweets behavior.")
    parser.add_argument("username", help="Account username, with or without @")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--samples", type=int, default=10)
    args = parser.parse_args()

    username = args.username.strip().lstrip("@")
    storage = StorageManager(project_root=Path(args.project_root), subsystem="historical")
    processor = TweetSetProcessor()

    raw_a = load_pages(storage, username, "UserTweets")
    raw_b = load_pages(storage, username, "UserTweetsAndReplies")
    set_a = processor.extract_tweets_from_raw(raw_a, username=username, source_endpoint="UserTweets")
    set_b = processor.extract_tweets_from_raw(raw_b, username=username, source_endpoint="UserTweetsAndReplies")

    describe_set("A UserTweets", set_a)
    describe_set("B UserTweetsAndReplies", set_b)

    keys_a = set(set_a)
    keys_b = set(set_b)
    ids_a = {key_by_id(tweet) for tweet in set_a.values() if key_by_id(tweet)}
    ids_b = {key_by_id(tweet) for tweet in set_b.values() if key_by_id(tweet)}

    print(f"key_intersection={len(keys_a & keys_b)} key_B_minus_A={len(keys_b - keys_a)}")
    print(f"id_intersection={len(ids_a & ids_b)} id_B_minus_A={len(ids_b - ids_a)}")

    print("\nB-A samples by canonical key:")
    for key in list(keys_b - keys_a)[: max(0, args.samples)]:
        tweet = set_b[key]
        print(json.dumps({
            "key": key,
            "id": tweet.get("id"),
            "account": tweet.get("account"),
            "type": tweet.get("type"),
            "in_reply_to_status_id": tweet.get("in_reply_to_status_id"),
            "text": str(tweet.get("text", ""))[:180],
        }, ensure_ascii=False))

    if not keys_b - keys_a and not ids_b - ids_a:
        print("\nConclusion: B is a subset of A for this data. Likely API reality or endpoint/context issue, not a set-operation key mismatch.")
    elif not keys_b - keys_a and ids_b - ids_a:
        print("\nConclusion: canonical key mismatch or author-id issue should be investigated.")
    else:
        print("\nConclusion: replies-only records exist; inspect export/state path if processed output is empty.")


if __name__ == "__main__":
    main()
```

## File: .gitignore
```
data/
config/config.json
__pycache__/
.env
```

## File: structure.txt
```
📁 TWEETER DATA FETCHING 4.0/
├── 📁 data/
│   ├── 📁 historical_live/
│   │   ├── 📁 logs/
│   │   ├── 📁 processed/
│   │   │   ├── 📁 1_user_tweets/
│   │   │   ├── 📁 2_user_tweets_and_replies/
│   │   │   ├── 📁 3_intersection/
│   │   │   ├── 📁 4_union/
│   │   │   └── 📁 5_replies_only/
│   │   ├── 📁 raw/
│   │   │   ├── 📁 UserTweets/
│   │   │   └── 📁 UserTweetsAndReplies/
│   │   ├── 📁 reports/
│   │   └── 📁 state/
│   └── 📁 search/
│       ├── 📁 raw/
│       └── 📁 state/
├── 📁 historical_scripts/
│   └── 📄 historical_runner.py
├── 📁 live_scripts/
│   ├── 📄 live_runner.py
│   ├── 📄 live_storage.py
│   └── 📄 viral_detector.py
├── 📁 search_scripts/
│   └── 📄 search_runner.py
└── 📁 shared/
    ├── 📁 auth/
    │   ├── 📄 __init__.py
    │   ├── 📄 session_updater.py
    │   └── 📄 setup_api_cookies.py
    ├── 📁 config/
    │   ├── 📄 __init__.py
    │   ├── 📄 config.json
    │   ├── 📄 search_config.json
    │   └── 📄 tier_config.py
    ├── 📁 core/
    │   ├── 📄 __init__.py
    │   ├── 📄 api_manager.py
    │   ├── 📄 fetcher_engine.py
    │   ├── 📄 set_operations.py
    │   └── 📄 windowing.py
    ├── 📁 data_pipeline/
    │   ├── 📄 __init__.py
    │   └── 📄 storage_manager.py
    ├── 📁 exporters/
    │   ├── 📄 __init__.py
    │   └── 📄 text_export_helper.py
    └── 📁 tools/
        ├── 📄 check_replies_parity.py
        └── 📄 diagnose_replies_only.py
```
