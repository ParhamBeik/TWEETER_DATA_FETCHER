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
