#!/usr/bin/env python3
from __future__ import annotations
"""
Monitor live profile timelines.

Code map:
- LiveMonitor polls configured accounts.
- New tweets are deduped through LiveStorageManager.
- ViralDetector checks fresh tweets for viral signals.
- Use --once for validation, otherwise this runs continuously.
"""


import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
except Exception:
    Console = None
    Panel = None
    Table = None


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.shared.config.account_tiers import get_priority_policy, load_tier_config, ordered_accounts
from src.shared.core.pagination_engine import FetcherEngine
from src.shared.core.tweet_processing_utils import TweetSetProcessor
from src.shared.core.tweet_processing_utils import window_cutoff
from src.shared.observability.pipeline_console import PipelineConsole
from src.pipelines.live.utils import LiveStorageManager
from src.pipelines.live.utils import ViralDetector


V4_PREFIX = "[V4]"


# Console output helpers (deprecated - using PipelineConsole) --------


# This class is deprecated; use PipelineConsole instead
class LiveConsole:
    """Rich-first console output for LiveMonitor, with plain-text fallback."""

    def __init__(self) -> None:
        self.rich_enabled = Console is not None
        self.console = Console() if self.rich_enabled else None

    def banner(self, title: str) -> None:
        if self.rich_enabled and Panel is not None:
            self.console.print(Panel.fit(title, title="V4 Live Monitor", border_style="magenta"))
        else:
            sep = "=" * 70
            print(f"{V4_PREFIX} {title}")
            print(sep)

    def info(self, message: str) -> None:
        if self.rich_enabled:
            self.console.print(f"[bold cyan]{V4_PREFIX}[/bold cyan] {message}")
        else:
            print(f"{V4_PREFIX} {message}", flush=True)

    def success(self, message: str) -> None:
        if self.rich_enabled:
            self.console.print(f"[bold green]{V4_PREFIX} \u2713 {message}[/bold green]")
        else:
            print(f"{V4_PREFIX} ✓ {message}", flush=True)

    def warning(self, message: str) -> None:
        if self.rich_enabled:
            self.console.print(f"[bold yellow]{V4_PREFIX} \u26a0 {message}[/bold yellow]")
        else:
            print(f"{V4_PREFIX} ⚠ {message}", flush=True)

    def error(self, message: str) -> None:
        if self.rich_enabled:
            self.console.print(f"[bold red]{V4_PREFIX} \u2717 {message}[/bold red]")
        else:
            print(f"{V4_PREFIX} ✗ {message}", flush=True)

    def account_summary(self, username: str, account_report: Dict[str, Any]) -> None:
        """Print a summary for a single account after a cycle."""
        status = account_report.get("status", "unknown")
        sets = account_report.get("sets", {})
        new_tweets = account_report.get("new_tweets", {})
        endpoints = account_report.get("endpoints", {})

        print()
        if self.rich_enabled and Table is not None:
            table = Table(title=f"Account: @{username} [{status.upper()}]", show_lines=False)
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="white")
            table.add_row("Status", status)
            table.add_row("Priority", str(account_report.get("priority", "")))
            table.add_row("Tweets (union)", str(sets.get("4_union", 0)))
            table.add_row("Tweets (intersection)", str(sets.get("3_intersection", 0)))
            table.add_row("Tweets (new this cycle)", str(new_tweets.get("new", 0)))
            table.add_row("Duplicates", str(new_tweets.get("duplicates", 0)))
            table.add_row("Viral Reports", str(new_tweets.get("viral_reports", 0)))
            for ep, ep_data in endpoints.items():
                pages = ep_data.get("pages_fetched", 0)
                http_status = ep_data.get("last_http_status", "N/A")
                reason = ep_data.get("outcome", "")
                table.add_row(f"Endpoint: {ep}", f"pages={pages} status={http_status}")
            self.console.print(table)
        else:
            print(f"{V4_PREFIX} @{username} [{status.upper()}]")
            print(f"{V4_PREFIX}   Priority: {account_report.get('priority', '')}")
            print(f"{V4_PREFIX}   Tweets (union): {sets.get('4_union', 0)}")
            print(f"{V4_PREFIX}   Tweets (intersection): {sets.get('3_intersection', 0)}")
            print(f"{V4_PREFIX}   New this cycle: {new_tweets.get('new', 0)}")
            print(f"{V4_PREFIX}   Duplicates: {new_tweets.get('duplicates', 0)}")
            print(f"{V4_PREFIX}   Viral Reports: {new_tweets.get('viral_reports', 0)}")
            for ep, ep_data in endpoints.items():
                pages = ep_data.get("pages_fetched", 0)
                http_status = ep_data.get("last_http_status", "N/A")
                print(f"{V4_PREFIX}   Endpoint {ep}: pages={pages} status={http_status}")


# Live polling ---------------------------------------------------------------


class LiveMonitor:
    """Poll UserTweets and UserTweetsAndReplies shallowly per account."""

    ENDPOINTS = ("UserTweets", "UserTweetsAndReplies")

    def __init__(self, config_path: str = "src/shared/config/config.json", validation_run_id: Optional[str] = None):
        self.project_root = Path(__file__).resolve().parents[3]
        self.validation_run_id = validation_run_id
        self.fetcher = FetcherEngine(config_path=config_path, subsystem="live", validation_run_id=validation_run_id)
        self.console = PipelineConsole(subsystem="live", verbosity="normal")
        self.api_manager = self.fetcher.api_manager
        self.config = self.api_manager.config
        self.account_map, self.priority_policies = load_tier_config(self.config)
        self.accounts = ordered_accounts(self.account_map)
        self.processor = TweetSetProcessor()
        self.live_storage = LiveStorageManager(self.project_root, data_root_override=self.fetcher.data_root)
        self.viral_detector = ViralDetector(config_path=config_path, storage=self.live_storage)
        viral_cfg = self.config.get("viral_detection", {})
        self.snapshot_min_delta = int(viral_cfg.get("snapshot_min_metric_delta", 25))
        self.snapshot_min_minutes = int(viral_cfg.get("snapshot_min_minutes", 10))

    @staticmethod
    def _compact_json(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    def _get_live_user_id(self, username: str) -> str:
        state = self.live_storage.account_state(username)
        cached_user_id = state.get("user_id")
        if cached_user_id:
            return str(cached_user_id)

        user_id = self.fetcher._get_user_id(username)
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
        watermark = self.live_storage.storage.get_fetch_watermark(username, endpoint)
        cutoff = window_cutoff(window_hours=live_window_hours, watermark=watermark, floor="hour")
        return self.fetcher._fetch_endpoint_result(
            account=username,
            user_id=user_id,
            endpoint=endpoint,
            max_pages=safety_cap_pages,
            window_days=None,
            cutoff=cutoff,
            force_refetch=bool(self.validation_run_id),
        )

    def _process_sets(self, username: str, endpoint_pages: Dict[str, List[Dict[str, Any]]], live_window_hours: int) -> Dict[str, List[Dict[str, Any]]]:
        set_a = self.processor.extract_tweets_from_raw(endpoint_pages.get("UserTweets", []), username=username, source_endpoint="UserTweets")
        set_b = self.processor.extract_tweets_from_raw(endpoint_pages.get("UserTweetsAndReplies", []), username=username, source_endpoint="UserTweetsAndReplies")
        outputs = {
            "1_user_tweets": list(set_a.values()),
            "2_user_tweets_and_replies": list(set_b.values()),
            "3_intersection": self.processor.get_intersection(set_a, set_b),
            "4_union": self.processor.get_union(set_a, set_b),
            "5_a_minus_b": self.processor.get_difference_a_minus_b(set_a, set_b),
            "6_b_minus_a": self.processor.get_difference_b_minus_a(set_a, set_b),
            "7_symmetric_difference": self.processor.get_symmetric_difference(set_a, set_b),
        }
        for key, tweets in outputs.items():
            self.live_storage.save_processed_set(username, key, tweets)
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
        if new_count > 0:
            self.console.success(f"New tweets for @{username}: {new_count}")
        if viral_reports > 0:
            self.console.info(f"Viral reports for @{username}: {viral_reports}")
        return {"new": new_count, "duplicates": duplicate_count, "viral_reports": viral_reports}

    def monitor_account(self, username: str) -> Dict[str, Any]:
        policy = get_priority_policy(username, self.account_map, self.priority_policies)
        live_window_hours = int(policy.get("live_window_hours", 24))
        prio = policy.get("priority", "")
        self.console.info(f"Starting @{username} (priority={prio}, window={live_window_hours}h)")
        result: Dict[str, Any] = {
            "account": username,
            "priority": policy.get("priority"),
            "started_at": datetime.utcnow().isoformat() + "Z",
            "endpoints": {},
        }
        try:
            self.console.info(f"Bootstrapping browser context for @{username}")
            bootstrap = self.fetcher.bootstrap_browser_context(username=username)
            result["browser_bootstrap"] = {
                "ok": bootstrap.ok,
                "route": bootstrap.route,
                "support_request_count": bootstrap.support_request_count,
                "error": bootstrap.error,
            }
            self.console.info(f"Resolving user ID for @{username}")
            user_id = self._get_live_user_id(username)
        except Exception as exc:
            self.console.error(f"User ID resolution failed for @{username}: {str(exc)[:200]}")
            result["status"] = "failed"
            result["reason"] = f"user_id_resolution_failed: {str(exc)[:300]}"
            self.live_storage.update_account_state(username, {"last_checked_at": datetime.utcnow().isoformat() + "Z", "last_status": "failed"})
            return result

        endpoint_pages: Dict[str, List[Dict[str, Any]]] = {}
        for endpoint in self.ENDPOINTS:
            self.console.info(f"Fetching {endpoint} for @{username}")
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
        endpoint_statuses = [str(row.get("status")) for row in result["endpoints"].values()]
        if any(status == "failed" for status in endpoint_statuses):
            result["status"] = "failed"
        elif any(status == "partial" for status in endpoint_statuses):
            result["status"] = "partial"
        else:
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
        self.console.banner(f"Cycle started: {len(selected)} account(s)")
        report = {
            "started_at": datetime.utcnow().isoformat() + "Z",
            "accounts": {},
            "summary": {"checked": 0, "skipped": 0, "failed": 0},
        }
        due_accounts: List[str] = []
        for username in selected:
            if not self.validation_run_id and not self.should_fetch_account(username):
                report["summary"]["skipped"] += 1
                continue
            report["summary"]["checked"] += 1
            due_accounts.append(username)

        user_ids: Dict[str, str] = {}
        for username in due_accounts:
            policy = get_priority_policy(username, self.account_map, self.priority_policies)
            report["accounts"][username] = {
                "account": username,
                "priority": policy.get("priority"),
                "started_at": datetime.utcnow().isoformat() + "Z",
                "endpoints": {},
            }
            try:
                bootstrap = self.fetcher.bootstrap_browser_context(username=username)
                report["accounts"][username]["browser_bootstrap"] = {
                    "ok": bootstrap.ok,
                    "route": bootstrap.route,
                    "support_request_count": bootstrap.support_request_count,
                    "error": bootstrap.error,
                }
                user_ids[username] = self._get_live_user_id(username)
            except Exception as exc:
                self.console.error(f"User ID resolution failed for @{username}: {str(exc)[:200]}")
                report["accounts"][username].update({
                    "status": "failed",
                    "reason": f"user_id_resolution_failed: {str(exc)[:300]}",
                    "finished_at": datetime.utcnow().isoformat() + "Z",
                })
                self.live_storage.update_account_state(username, {"last_checked_at": datetime.utcnow().isoformat() + "Z", "last_status": "failed"})
                report["summary"]["failed"] += 1

        endpoint_pages_by_account: Dict[str, Dict[str, List[Dict[str, Any]]]] = {username: {} for username in user_ids}
        for endpoint in self.ENDPOINTS:
            for username, user_id in user_ids.items():
                policy = get_priority_policy(username, self.account_map, self.priority_policies)
                endpoint_result = self._fetch_live_endpoint(
                    username,
                    user_id,
                    endpoint,
                    live_window_hours=int(policy.get("live_window_hours", 24)),
                    safety_cap_pages=self.fetcher.pagination_safety_cap_pages,
                )
                report["accounts"][username]["endpoints"][endpoint] = {k: v for k, v in endpoint_result.items() if k != "pages"}
                endpoint_pages_by_account[username][endpoint] = endpoint_result.get("pages", [])
                self.api_manager.human_delay("between_accounts")

        for username, endpoint_pages in endpoint_pages_by_account.items():
            policy = get_priority_policy(username, self.account_map, self.priority_policies)
            sets = self._process_sets(username, endpoint_pages, int(policy.get("live_window_hours", 24)))
            new_stats = self._handle_new_tweets(username, sets["4_union"])
            account_report = report["accounts"][username]
            account_report["sets"] = {key: len(value) for key, value in sets.items()}
            account_report["new_tweets"] = new_stats
            statuses = [str(row.get("status")) for row in account_report["endpoints"].values()]
            account_report["status"] = "failed" if any(status == "failed" for status in statuses) else ("partial" if any(status == "partial" for status in statuses) else "completed")
            account_report["finished_at"] = datetime.utcnow().isoformat() + "Z"
            self.live_storage.update_account_state(
                username,
                {
                    "last_checked_at": account_report["finished_at"],
                    "last_status": account_report["status"],
                    "last_counts": account_report["sets"],
                },
            )
            if account_report.get("status") != "completed":
                report["summary"]["failed"] += 1
        report["finished_at"] = datetime.utcnow().isoformat() + "Z"

        # Print summary after cycle
        self.console.banner(f"Cycle complete: {report['summary']}")
        for username in selected:
            if username in report.get("accounts", {}):
                self.console.account_summary(username, report["accounts"][username])
        if not report.get("accounts"):
            self.console.warning("No accounts were processed in this cycle")

        return report

    def run_continuous(self, only_accounts: Optional[List[str]] = None, check_interval: int = 60) -> None:
        self.console.banner("Starting v4 live monitor. Press Ctrl+C to stop.")
        while True:
            report = self.run_cycle(only_accounts=only_accounts)
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
    parser.add_argument("--config", default="src/shared/config/config.json")
    parser.add_argument("--account", action="append", dest="accounts", help="Limit to one account; can be repeated.")
    parser.add_argument("--once", action="store_true", help="Run one internal validation cycle instead of continuous mode.")
    parser.add_argument("--check-interval", type=int, default=60)
    parser.add_argument("--validation-run-id", help="Write isolated output under data/validation/<run_id>/ and bypass polling state.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    monitor = LiveMonitor(config_path=args.config, validation_run_id=args.validation_run_id)
    if args.once:
        report = monitor.run_cycle(only_accounts=args.accounts)
    else:
        monitor.run_continuous(only_accounts=args.accounts, check_interval=args.check_interval)


if __name__ == "__main__":
    main()
