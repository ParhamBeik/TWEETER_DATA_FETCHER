#!/usr/bin/env python3
"""Live profile-timeline monitor.

Polls due accounts on a loop (or a single cycle with ``--once``), writes raw
pages and processed snapshots under ``data/historical_live/``, and dedupes
tweets it has already seen.

    python -m fetcher.live --account elonmusk --once
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fetcher.config import PROJECT_ROOT, get_priority_policy, load_tier_config, ordered_accounts
from fetcher.observability import PipelineConsole, attach_run_id
from fetcher.processing import TweetSetProcessor, window_cutoff
from fetcher.storage import StorageManager
from fetcher.timeline import FetcherEngine


"""
Isolated v4 live-monitoring storage and viral-report helpers.
"""




class LiveStorageManager:
    """Keep live state and outputs separate from historical sync state."""

    def __init__(
        self,
        project_root: Optional[Path] = None,
        timezone: str = "Asia/Tehran",
        data_root_override: Optional[Path] = None,
    ):
        self.project_root = project_root or PROJECT_ROOT
        self.storage = StorageManager(
            base_dir=self.project_root,
            timezone=timezone,
            subsystem="historical_live",
            data_root_override=data_root_override,
        )
        self.data_root = self.storage.data_root
        self.raw_root = self.data_root / "raw"
        self.processed_root = self.data_root / "processed"
        self.reports_root = self.data_root / "reports"
        self.state_dir = self.data_root / "state"
        self.live_state_file = self.state_dir / "live_state.json"
        self.seen_tweets_file = self.state_dir / "seen_tweets.json"
        self._ensure_dirs()
        self.live_state = self._load_json(self.live_state_file, {})
        self.seen_tweets = self._load_json(self.seen_tweets_file, {})

    def _ensure_dirs(self) -> None:
        for path in [self.raw_root, self.processed_root, self.reports_root, self.state_dir]:
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
        return self.storage._now()

    def batch_name(self) -> str:
        return self.storage._batch_name(self.now())

    def raw_batch_dir(self, username: str, endpoint: str) -> Path:
        target = self.raw_root / endpoint / self.safe_slug(username.lower()) / self.batch_name()
        target.mkdir(parents=True, exist_ok=True)
        return target

    def save_raw_page(self, username: str, endpoint: str, page_number: int, payload: Dict[str, Any]) -> Path:
        return self.storage.save_raw_page(self.raw_batch_dir(username, endpoint), page_number, payload)

    @staticmethod
    def _account_key(username: str) -> str:
        key = username.lower().lstrip("@")
        if key.startswith("_"):
            # Leading underscore is reserved for internal entries (e.g. "_scheduler")
            # stored in the same flat live_state dict; a real account normalizing to
            # one would silently clobber that entry, so fail loudly instead.
            raise ValueError(f"account handle {username!r} normalizes to a reserved key ({key!r})")
        return key

    def account_state(self, username: str) -> Dict[str, Any]:
        key = self._account_key(username)
        state = self.live_state.get(key, {})
        return state if isinstance(state, dict) else {}

    def update_account_state(self, username: str, updates: Dict[str, Any]) -> Path:
        key = self._account_key(username)
        current = self.account_state(username)
        current.update(updates)
        self.live_state[key] = current
        return self._save_json(self.live_state_file, self.live_state)

    def scheduler_state(self) -> Dict[str, Any]:
        state = self.live_state.get("_scheduler", {})
        return state if isinstance(state, dict) else {}

    def update_scheduler_state(self, updates: Dict[str, Any]) -> Path:
        current = self.scheduler_state()
        current.update(updates)
        self.live_state["_scheduler"] = current
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

    def save_processed_set(self, username: str, set_name: str, tweets: List[Dict[str, Any]]) -> List[Path]:
        # Merge into the shared historical_live store (same writer historical uses),
        # producing {folder}.json (merged by tweet id) + per-Jalali-date .txt files.
        # This unifies live with historical so a live run accumulates instead of
        # overwriting the previously-merged historical set.
        return self.storage.save_processed_set_merged(tweets or [], set_name, username)


# Live polling ---------------------------------------------------------------


class LiveMonitor:
    """Poll UserTweets shallowly per account."""

    ENDPOINTS = ("UserTweets",)
    QUARANTINE_FAILURE_THRESHOLD = 3
    RATE_LIMIT_RESERVE = 5

    def __init__(self, config_path: Optional[str] = None):
        self.project_root = PROJECT_ROOT
        self.fetcher = FetcherEngine(config_path=config_path, subsystem="live")
        self.run_id = self.fetcher.storage_manager.create_run_id()
        attach_run_id(self.run_id)
        self.fetcher.recorder.run_id = self.run_id
        self.console = PipelineConsole(subsystem="live", verbosity="normal")
        self.api_manager = self.fetcher.api_manager
        self.config = self.api_manager.config
        self.account_map, self.priority_policies = load_tier_config(self.config)
        self.accounts = ordered_accounts(self.account_map)
        self.processor = TweetSetProcessor()
        self.live_storage = LiveStorageManager(self.project_root, data_root_override=self.fetcher.data_root)

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

    def _record_resolution_success(self, username: str) -> None:
        self.live_storage.update_account_state(
            username,
            {"availability_failure_count": 0, "last_availability_evidence": None},
        )

    def _record_resolution_failure(self, username: str, reason: str) -> Dict[str, Any]:
        state = self.live_storage.account_state(username)
        count = int(state.get("availability_failure_count", 0) or 0) + 1
        now = datetime.utcnow().isoformat() + "Z"
        updates: Dict[str, Any] = {
            "last_checked_at": now,
            "last_status": "failed",
            "availability_failure_count": count,
            "last_availability_evidence": str(reason)[:300],
        }
        if count >= self.QUARANTINE_FAILURE_THRESHOLD:
            updates.update({
                "quarantined": True,
                "quarantined_at": now,
                "quarantine_reason": str(reason)[:300],
            })
            self.console.warning(
                f"quarantined @{username} after {count} consecutive resolution failures: {str(reason)[:200]}"
            )
        self.live_storage.update_account_state(username, updates)
        return updates

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

    def _record_endpoint_result(self, username: str, result: Dict[str, Any]) -> None:
        updates = {
            "last_status": result["status"],
            "last_counts": result["sets"],
        }
        if result["status"] == "completed":
            updates["last_checked_at"] = result["finished_at"]
        self.live_storage.update_account_state(username, updates)

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
            force_refetch=False,
        )

    def _process_sets(self, username: str, endpoint_pages: Dict[str, List[Dict[str, Any]]], live_window_hours: int) -> Dict[str, List[Dict[str, Any]]]:
        set_a = self.processor.extract_tweets_from_raw(endpoint_pages.get("UserTweets", []), username=username, source_endpoint="UserTweets")
        outputs = {"4_union": list(set_a.values())}
        self.live_storage.save_processed_set(username, "4_union", outputs["4_union"])
        raw_keep = int((self.config.get("storage") or {}).get("raw_batch_retention_count", 0) or 0)
        if raw_keep > 0:
            for endpoint in self.ENDPOINTS:
                self.live_storage.storage.prune_raw_batches(endpoint, username, keep=raw_keep)
        return outputs

    def _handle_new_tweets(self, username: str, tweets: List[Dict[str, Any]]) -> Dict[str, int]:
        new_count = 0
        duplicate_count = 0
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
        if new_count > 0:
            self.console.success(f"New tweets for @{username}: {new_count}")
        return {"new": new_count, "duplicates": duplicate_count}

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
            self.console.info(f"Resolving user ID for @{username}")
            user_id = self._get_live_user_id(username)
        except Exception as exc:
            self.console.error(f"User ID resolution failed for @{username}: {str(exc)[:200]}")
            result["status"] = "failed"
            result["reason"] = f"user_id_resolution_failed: {str(exc)[:300]}"
            result["availability"] = self._record_resolution_failure(username, result["reason"])
            return result
        self._record_resolution_success(username)

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
        self._record_endpoint_result(username, result)
        return result

    def _available_timeline_requests(self) -> int:
        state = self.api_manager.rate_limits.get("UserTweets", {})
        limit = int(state.get("limit", 0) or 0)
        remaining = int(state.get("remaining", limit) or 0)
        reset = int(state.get("reset", 0) or 0)
        if reset and reset <= int(time.time()):
            remaining = limit
        return max(0, remaining - self.RATE_LIMIT_RESERVE)

    def _rotate_due_accounts(self, accounts: List[str]) -> List[str]:
        if not accounts:
            return []
        cursor = str(self.live_storage.scheduler_state().get("next_account") or "")
        start = accounts.index(cursor) if cursor in accounts else 0
        return accounts[start:] + accounts[:start]

    def _admit_due_accounts(self, accounts: List[str], *, rotate: bool) -> tuple[List[str], List[str]]:
        ordered = self._rotate_due_accounts(accounts) if rotate else list(accounts)
        admitted = ordered[:self._available_timeline_requests()]
        deferred = ordered[len(admitted):]
        if admitted and rotate:
            next_index = (self.accounts.index(admitted[-1]) + 1) % len(self.accounts)
            self.live_storage.update_scheduler_state({"next_account": self.accounts[next_index]})
        return admitted, deferred

    def run_cycle(self, only_accounts: Optional[List[str]] = None) -> Dict[str, Any]:
        selected = only_accounts or self.accounts
        self.console.banner(f"Cycle started: {len(selected)} account(s)")
        self.fetcher.recorder.emit("cycle_start", accounts=selected)
        report = {
            "started_at": datetime.utcnow().isoformat() + "Z",
            "accounts": {},
            "summary": {
                "eligible": 0, "checked": 0, "skipped": 0, "failed": 0,
                "quarantined": 0, "deferred": 0,
            },
        }
        due_accounts: List[str] = []
        for username in selected:
            availability = self.live_storage.account_state(username)
            if availability.get("quarantined") is True:
                report["summary"]["quarantined"] += 1
                report["accounts"][username] = {
                    "account": username,
                    "status": "quarantined",
                    "reason": availability.get("quarantine_reason") or "target unavailable",
                    "last_evidence": availability.get("last_availability_evidence"),
                    "quarantined_at": availability.get("quarantined_at"),
                    "endpoints": {},
                }
                continue
            if not self.should_fetch_account(username):
                report["summary"]["skipped"] += 1
                continue
            report["summary"]["eligible"] += 1
            due_accounts.append(username)

        admitted_accounts, deferred_accounts = self._admit_due_accounts(
            due_accounts, rotate=only_accounts is None
        )
        report["summary"]["checked"] = len(admitted_accounts)
        for username in deferred_accounts:
            report["accounts"][username] = {
                "account": username,
                "status": "deferred",
                "reason": "rate_budget_reserve",
                "endpoints": {},
            }
        report["summary"]["deferred"] = len(deferred_accounts)

        user_ids: Dict[str, str] = {}
        for username in admitted_accounts:
            policy = get_priority_policy(username, self.account_map, self.priority_policies)
            report["accounts"][username] = {
                "account": username,
                "priority": policy.get("priority"),
                "started_at": datetime.utcnow().isoformat() + "Z",
                "endpoints": {},
            }
            try:
                user_ids[username] = self._get_live_user_id(username)
                self._record_resolution_success(username)
            except Exception as exc:
                self.console.error(f"User ID resolution failed for @{username}: {str(exc)[:200]}")
                report["accounts"][username].update({
                    "status": "failed",
                    "reason": f"user_id_resolution_failed: {str(exc)[:300]}",
                    "finished_at": datetime.utcnow().isoformat() + "Z",
                })
                report["accounts"][username]["availability"] = self._record_resolution_failure(
                    username, report["accounts"][username]["reason"]
                )
                report["summary"]["failed"] += 1

        endpoint_pages_by_account: Dict[str, Dict[str, List[Dict[str, Any]]]] = {username: {} for username in user_ids}
        mid_loop_deferred: set[str] = set()
        for endpoint in self.ENDPOINTS:
            for idx, (username, user_id) in enumerate(user_ids.items()):
                if username in mid_loop_deferred:
                    continue
                if self._available_timeline_requests() <= 0:
                    report["accounts"][username].update(
                        {"status": "deferred", "reason": "rate_budget_reserve"}
                    )
                    mid_loop_deferred.add(username)
                    # Was already counted in "checked" at admission time; move it to
                    # "deferred" instead of double-counting both.
                    report["summary"]["checked"] -= 1
                    report["summary"]["deferred"] += 1
                    continue
                if idx > 0:
                    self.api_manager.human_delay("between_accounts")
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

        for username, endpoint_pages in endpoint_pages_by_account.items():
            if report["accounts"][username].get("status") == "deferred":
                continue
            policy = get_priority_policy(username, self.account_map, self.priority_policies)
            sets = self._process_sets(username, endpoint_pages, int(policy.get("live_window_hours", 24)))
            new_stats = self._handle_new_tweets(username, sets["4_union"])
            account_report = report["accounts"][username]
            account_report["sets"] = {key: len(value) for key, value in sets.items()}
            account_report["new_tweets"] = new_stats
            statuses = [str(row.get("status")) for row in account_report["endpoints"].values()]
            account_report["status"] = "failed" if any(status == "failed" for status in statuses) else ("partial" if any(status == "partial" for status in statuses) else "completed")
            account_report["finished_at"] = datetime.utcnow().isoformat() + "Z"
            self._record_endpoint_result(username, account_report)
            if account_report.get("status") != "completed":
                report["summary"]["failed"] += 1
        report["finished_at"] = datetime.utcnow().isoformat() + "Z"

        # Print summary after cycle
        self.console.banner(f"Cycle complete: {report['summary']}")
        for username in selected:
            if username in report.get("accounts", {}):
                self.console.account_summary_table(username, report["accounts"][username])
        if not report.get("accounts"):
            self.console.warning("No accounts were processed in this cycle")

        report_id = (
            f"{getattr(self, 'run_id', None) or 'live'}_"
            f"live_{datetime.utcnow().strftime('%Y%m%dT%H%M%S%fZ')}"
        )
        report["report_id"] = report_id
        report_path = self.live_storage.storage.save_run_report_json(report, report_id)
        report["report_path"] = str(report_path)
        self.fetcher.recorder.emit(
            "cycle_end", summary=report["summary"], report_path=str(report_path)
        )
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
    parser.add_argument("--config")
    parser.add_argument("--account", action="append", dest="accounts", help="Limit to one account; can be repeated.")
    parser.add_argument("--once", action="store_true", help="Run one cycle instead of continuous mode.")
    parser.add_argument("--check-interval", type=int, default=60)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    monitor = LiveMonitor(config_path=args.config)
    if args.once:
        report = monitor.run_cycle(only_accounts=args.accounts)
    else:
        monitor.run_continuous(only_accounts=args.accounts, check_interval=args.check_interval)


if __name__ == "__main__":
    main()
