#!/usr/bin/env python3
"""
Twitter Live Tweet Monitor with integrated viral candidate detection.

Continuously polls configured accounts, saves normal output to TWEETS/, writes
per-interval live candidate snapshots to VIRAL TWEETS/, and uses TweetDetail for
budget-aware viral rechecks.
"""

import json
import math
import os
import random
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from fetch_historical_tweets import SEP, TwitterUnifiedFetcher

try:
    import jdatetime
    import pytz
except ImportError:
    print("ERROR: Missing dependencies. Run: pip3 install jdatetime pytz")
    sys.exit(1)

# ============================================================================
# CONFIGURATION - edit only this section
# ============================================================================

ACCOUNTS = ["elonmusk", "whale_alert", "paulg"]

# Target delay between account polls. The monitor stretches this when rate-limit
# headroom is low after viral checks.
LIVE_POLL_SECONDS = 120
MIN_LIVE_POLL_SECONDS = 45
MAX_LIVE_POLL_SECONDS = 300

# Stop monitoring after this many consecutive account-level API errors.
MAX_CONSECUTIVE_ERRORS = 5

# Output root for live candidate snapshots and viral decisions.
VIRAL_CANDIDATE_FOLDER = "VIRAL TWEETS"

# ============================================================================
# DO NOT EDIT BELOW THIS LINE
# ============================================================================

TIMEZONE = "Asia/Tehran"
SNAPSHOT_SEPARATOR = "\n" + SEP + "\n" + SEP + "\n\n"


class TwitterLiveMonitor(TwitterUnifiedFetcher):
    """Poll live timelines and run budget-aware viral detection in one process."""

    def __init__(self, config_path: str = "config.json"):
        super().__init__(config_path=config_path)
        self.user_ids: Dict[str, str] = {}
        self.errors: Dict[str, int] = {}
        self.base_dir = Path(__file__).parent
        self.tz = pytz.timezone(TIMEZONE)
        self.viral_root = self.base_dir / VIRAL_CANDIDATE_FOLDER
        self.viral_root.mkdir(exist_ok=True)

        viral_config = self.config.get("viral_config", {})
        self.window_days = int(viral_config.get("window_days", 7))
        self.threshold_percentile = int(viral_config.get("threshold_percentile", 95))
        self.recheck_hours = int(viral_config.get("recheck_hours", 24))
        self.intervals_minutes = [
            int(value) for value in viral_config.get("intervals_minutes", [5, 30, 120, 600])
        ]
        self.max_detail_per_run = int(viral_config.get("max_detail_refreshes_per_run", 30))
        self.composite_cutoff = float(viral_config.get("composite_score_cutoff", 1.0))
        self.delta_percentile_cutoff = float(viral_config.get("delta_percentile_cutoff", 0.80))
        self.delta_weight = float(viral_config.get("delta_score_weight", 0.70))
        self.history_weight = float(viral_config.get("history_score_weight", 0.30))
        self.budget_mode = str(viral_config.get("api_budget_mode", "balanced")).lower()

        self.next_viral_due: Dict[int, float] = {
            interval: time.time() + interval * 60 for interval in self.intervals_minutes
        }
        self.baseline_cache: Dict[str, Tuple[Dict, float]] = {}
        self.baseline_cache_ttl = 3600
        self.detail_snapshots: Dict[str, List[Dict]] = defaultdict(list)
        self.viral_ids: Set[str] = self._load_existing_viral_ids()

    # ------------------------------------------------------------------
    # Live timeline extraction
    # ------------------------------------------------------------------

    def _extract_timeline_items(self, data: Dict) -> List[Dict]:
        """Parse timeline entries and conversation entries into tweet dictionaries."""
        items: Dict[str, Dict] = {}
        instructions = (
            data.get("data", {})
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
                entry_id = entry.get("entryId", "")

                if entry_id.startswith("tweet-"):
                    tweet_obj = (
                        entry.get("content", {})
                        .get("itemContent", {})
                        .get("tweet_results", {})
                        .get("result", {})
                    )
                    unwrapped = self._unwrap_tweet_result(tweet_obj) if tweet_obj else None
                    parsed = self._parse_tweet(unwrapped) if unwrapped else None
                    if parsed and parsed.get("id"):
                        items[parsed["id"]] = parsed
                    continue

                if entry_id.startswith("profile-conversation-"):
                    for item_entry in entry.get("content", {}).get("items", []):
                        tweet_obj = (
                            item_entry.get("item", {})
                            .get("itemContent", {})
                            .get("tweet_results", {})
                            .get("result", {})
                        )
                        unwrapped = self._unwrap_tweet_result(tweet_obj) if tweet_obj else None
                        parsed = self._parse_tweet(unwrapped) if unwrapped else None
                        if parsed and parsed.get("id"):
                            items[parsed["id"]] = parsed

        return list(items.values())

    def _extract_tweet_detail_item(self, data: Dict, tweet_id: str) -> Optional[Dict]:
        instructions = (
            data.get("data", {})
            .get("threaded_conversation_with_injections_v2", {})
            .get("instructions", [])
        )
        for inst in instructions:
            for entry in inst.get("entries", []):
                tweet_obj = (
                    entry.get("content", {})
                    .get("itemContent", {})
                    .get("tweet_results", {})
                    .get("result", {})
                )
                unwrapped = self._unwrap_tweet_result(tweet_obj) if tweet_obj else None
                parsed = self._parse_tweet(unwrapped) if unwrapped else None
                if parsed and str(parsed.get("id")) == str(tweet_id):
                    return parsed
        return None

    def _resolve_user_id(self, username: str) -> Optional[str]:
        if username in self.user_ids:
            return self.user_ids[username]
        user_id = self.get_user_id(username)
        if user_id:
            self.user_ids[username] = user_id
            self.errors[username] = 0
        return user_id

    def _existing_primary_ids(self, username: str) -> Set[str]:
        account_folder = self.tweets_root / username.upper()
        if not account_folder.exists():
            return set()

        ids: Set[str] = set()
        for file_path in account_folder.glob("*.txt"):
            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue
            for block in self._split_existing_blocks(content):
                primary_id = self._extract_primary_id_from_block(block)
                if primary_id:
                    ids.add(primary_id.split("retweet:", 1)[-1])
        return ids

    def poll_account_once(self, username: str) -> List[Dict]:
        """Fetch one live batch, save normal output, and return newly added items."""
        user_id = self._resolve_user_id(username)
        if not user_id:
            self.errors[username] = self.errors.get(username, 0) + 1
            return []

        all_items: Dict[str, Dict] = {}

        tweets_data = self.get_user_tweets(user_id)
        if tweets_data:
            for item in self._extract_timeline_items(tweets_data):
                all_items[item["id"]] = item

        if self.errors.get(username, 0) < MAX_CONSECUTIVE_ERRORS and self._endpoint_headroom("UserTweetsAndReplies") > 3:
            replies_data = self.get_user_tweets_and_replies(user_id, username=username)
            if replies_data:
                for item in self._extract_timeline_items(replies_data):
                    all_items[item["id"]] = item

        account_items = [
            item for item in all_items.values()
            if self._is_account_timeline_item(item, username)
        ]
        if not account_items:
            print(f"  - No new tweets for @{username}")
            self.errors[username] = 0
            return []

        existing_ids = self._existing_primary_ids(username)
        new_items = [
            item for item in account_items
            if self._primary_item_id(item) and self._primary_item_id(item) not in existing_ids
        ]

        self._save_to_daily_file(username, account_items)
        if new_items:
            print(f"  + @{username}: saved {len(new_items)} new candidate item(s)")
            self._save_candidate_snapshots(username, new_items)
        else:
            print(f"  - No new tweets for @{username}")

        self.errors[username] = 0
        return new_items

    # ------------------------------------------------------------------
    # Snapshot output and parsing
    # ------------------------------------------------------------------

    def _interval_label(self, minutes: int) -> str:
        return f"{minutes} Minute" if minutes == 1 else f"{minutes} Minutes"

    def _now_jalali_filename(self) -> str:
        now = datetime.now(self.tz)
        return jdatetime.datetime.fromgregorian(datetime=now).strftime("%Y-%m-%d %H:%M:%S")

    def _save_candidate_snapshots(self, account: str, tweets: List[Dict]) -> None:
        timestamp = self._now_jalali_filename()
        for interval in self.intervals_minutes:
            folder = self.viral_root / self._interval_label(interval) / account.upper()
            folder.mkdir(parents=True, exist_ok=True)
            file_path = folder / f"{timestamp}.txt"
            lines = [
                f"# Live candidate snapshot",
                f"# Account: @{account}",
                f"# Interval: {self._interval_label(interval)}",
                f"# Snapshot: {timestamp}",
                "",
            ]
            for tweet in tweets:
                lines.append(self._format_candidate_record(tweet, account))
            file_path.write_text("\n".join(lines), encoding="utf-8")

    def _format_candidate_record(self, tweet: Dict, account: str) -> str:
        engagement = tweet.get("engagement", {})
        lines = [
            "CANDIDATE TWEET",
            "",
            f"Account: @{account}",
            f"Type: {tweet.get('type', 'tweet')}",
            f"Tweet ID: {self._primary_item_id(tweet)}",
            f"Created: {tweet.get('jalali_time', '')}",
            f"Snapshot: {self._now_jalali_filename()}",
            "",
            self._wrap(tweet.get("text", ""), width=90),
            "",
            (
                f"Metrics: replies={engagement.get('replies', '0')} "
                f"retweets={engagement.get('retweets', '0')} "
                f"likes={engagement.get('likes', '0')} "
                f"quotes={engagement.get('quotes', '0')} "
                f"bookmarks={engagement.get('bookmarks', '0')} "
                f"views={engagement.get('views', '0')}"
            ),
            f"Link: {tweet.get('link', '')}",
            SEP,
            SEP,
            "",
        ]
        return "\n".join(lines)

    def _load_live_candidate_ids(self, interval: int) -> Dict[str, Set[str]]:
        cutoff = datetime.now(self.tz) - timedelta(days=self.window_days)
        interval_root = self.viral_root / self._interval_label(interval)
        candidates: Dict[str, Set[str]] = defaultdict(set)
        if not interval_root.exists():
            return candidates

        for account_folder in interval_root.iterdir():
            if not account_folder.is_dir():
                continue
            account = account_folder.name.lower()
            for file_path in account_folder.glob("*.txt"):
                if " VIRAL" in file_path.stem:
                    continue
                snapshot_time = self._parse_jalali_filename(file_path.stem)
                if snapshot_time and snapshot_time < cutoff:
                    continue
                try:
                    content = file_path.read_text(encoding="utf-8")
                except Exception:
                    continue
                for match in re.finditer(r"Tweet ID:\s*(\d+)", content):
                    candidates[account].add(match.group(1))
        return candidates

    def _parse_jalali_filename(self, value: str) -> Optional[datetime]:
        try:
            jdt = jdatetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            return self.tz.localize(jdt.togregorian())
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Baselines, scoring, and viral output
    # ------------------------------------------------------------------

    def _tweet_metrics(self, tweet: Dict) -> Dict[str, int]:
        engagement = tweet.get("engagement", {})
        return {
            "replies": self._safe_int(engagement.get("replies")),
            "retweets": self._safe_int(engagement.get("retweets")),
            "likes": self._safe_int(engagement.get("likes")),
            "quotes": self._safe_int(engagement.get("quotes")),
            "bookmarks": self._safe_int(engagement.get("bookmarks")),
            "views": self._safe_int(engagement.get("views")),
        }

    def _safe_int(self, value) -> int:
        try:
            return int(str(value).replace(",", ""))
        except Exception:
            return 0

    def _engagement_rate_from_metrics(self, metrics: Dict[str, int]) -> float:
        actions = metrics["likes"] + metrics["retweets"] + metrics["replies"]
        return actions / max(metrics["views"], 1)

    def _load_tweets_for_account(self, account: str, days: int) -> List[Dict]:
        account_folder = self.tweets_root / account.upper()
        if not account_folder.exists():
            return []

        cutoff = datetime.now(self.tz) - timedelta(days=days)
        tweets = []
        for file_path in sorted(account_folder.glob("*.txt")):
            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue
            for block in self._split_existing_blocks(content):
                tweet = self._extract_tweet_from_block(block)
                if tweet and tweet["timestamp"] >= cutoff:
                    tweet["account"] = account
                    tweets.append(tweet)
        return tweets

    def _extract_tweet_from_block(self, block: str) -> Optional[Dict]:
        id_match = re.search(r"🆔 Tweet ID:\s*(\d+)", block)
        time_match = re.search(r"📅(?: Retweeted at:)?\s*(\d{4}/\d{2}/\d{2}\s*-\s*\d{2}:\d{2})", block)
        metrics_match = re.search(
            r"💬\s*(\d+)\s+🔁\s*(\d+)\s+❤️\s*(\d+)\s+💬\s*(\d+)\s+🔖\s*(\d+)\s+👁\s*(\d+)",
            block,
        )
        if not (id_match and time_match and metrics_match):
            return None

        timestamp = self._parse_jalali_datetime(time_match.group(1))
        if not timestamp:
            return None

        replies, retweets, likes, quotes, bookmarks, views = map(int, metrics_match.groups())
        return {
            "id": id_match.group(1),
            "timestamp": timestamp,
            "metrics": {
                "replies": replies,
                "retweets": retweets,
                "likes": likes,
                "quotes": quotes,
                "bookmarks": bookmarks,
                "views": views,
            },
        }

    def _parse_jalali_datetime(self, value: str) -> Optional[datetime]:
        try:
            match = re.match(r"(\d{4})/(\d{2})/(\d{2})\s*-\s*(\d{2}):(\d{2})", value)
            if not match:
                return None
            year, month, day, hour, minute = map(int, match.groups())
            return self.tz.localize(jdatetime.datetime(year, month, day, hour, minute).togregorian())
        except Exception:
            return None

    def _get_account_baseline(self, account: str) -> Dict:
        cached = self.baseline_cache.get(account)
        if cached and time.time() - cached[1] < self.baseline_cache_ttl:
            return cached[0]

        tweets = self._load_tweets_for_account(account, self.window_days)
        rates = [self._engagement_rate_from_metrics(tweet["metrics"]) for tweet in tweets]
        rates.sort()

        def percentile(data: List[float], p: int) -> float:
            if not data:
                return 0.01
            k = (len(data) - 1) * p / 100
            f = math.floor(k)
            c = math.ceil(k)
            if f == c:
                return data[int(k)]
            return data[int(f)] * (c - k) + data[int(c)] * (k - f)

        baseline = {
            "p50": percentile(rates, 50),
            "p75": percentile(rates, 75),
            "p90": percentile(rates, 90),
            "p95": percentile(rates, 95),
            "p99": percentile(rates, 99),
            "count": len(rates),
        }
        self.baseline_cache[account] = (baseline, time.time())
        return baseline

    def _weighted_delta(self, previous: Dict[str, int], current: Dict[str, int]) -> float:
        weights = {
            "likes": 1.0,
            "retweets": 3.0,
            "replies": 2.0,
            "quotes": 2.5,
            "bookmarks": 1.5,
            "views": 0.02,
        }
        return sum(max(0, current[key] - previous.get(key, 0)) * weight for key, weight in weights.items())

    def _detail_budget(self) -> int:
        configured_limit = self.config.get("rate_limits", {}).get("TweetDetail", {}).get("limit", 150)
        remaining = self._endpoint_headroom("TweetDetail", default=configured_limit)
        ratio = 0.5
        if self.budget_mode == "conservative":
            ratio = 0.25
        elif self.budget_mode == "aggressive":
            ratio = 0.75
        return max(0, min(self.max_detail_per_run, int(remaining * ratio)))

    def _endpoint_headroom(self, endpoint: str, default: int = 10) -> int:
        state = self.rate_limit_state.get(endpoint)
        if not state:
            return default
        reset_time = state.get("reset", 0)
        if reset_time and reset_time <= time.time():
            limit = self.config.get("rate_limits", {}).get(endpoint, {}).get("limit", default)
            return int(limit)
        return int(state.get("remaining", default))

    def _refresh_candidates(self, interval: int) -> List[Dict]:
        if not self.TWEET_DETAIL_QUERY_ID:
            return []

        candidate_ids = self._load_live_candidate_ids(interval)
        budget = self._detail_budget()
        if not candidate_ids or budget <= 0:
            return []

        refreshed: List[Dict] = []
        for account, ids in sorted(candidate_ids.items()):
            for tweet_id in sorted(ids):
                if budget <= 0:
                    return refreshed
                data = self.get_tweet_detail(tweet_id)
                if not data:
                    continue
                tweet = self._extract_tweet_detail_item(data, tweet_id)
                if not tweet:
                    continue
                tweet["account"] = account
                metrics = self._tweet_metrics(tweet)
                snapshot = {
                    "id": tweet_id,
                    "account": account,
                    "tweet": tweet,
                    "metrics": metrics,
                    "timestamp": datetime.now(self.tz),
                }
                self.detail_snapshots[tweet_id].append(snapshot)
                refreshed.append(snapshot)
                budget -= 1
        return refreshed

    def _score_refreshed(self, refreshed: List[Dict]) -> List[Dict]:
        if not refreshed:
            return []

        deltas = []
        for snapshot in refreshed:
            history = self.detail_snapshots.get(snapshot["id"], [])
            previous = history[-2]["metrics"] if len(history) >= 2 else snapshot["metrics"]
            snapshot["delta"] = self._weighted_delta(previous, snapshot["metrics"])
            deltas.append(snapshot["delta"])

        sorted_deltas = sorted(deltas)
        viral = []
        for snapshot in refreshed:
            account = snapshot["account"]
            baseline = self._get_account_baseline(account)
            threshold = max(baseline.get(f"p{self.threshold_percentile}", 0.01), 0.0001)
            engagement_rate = self._engagement_rate_from_metrics(snapshot["metrics"])
            history_score = engagement_rate / threshold
            delta_percentile = self._rank_percentile(sorted_deltas, snapshot["delta"])
            composite = self.history_weight * history_score + self.delta_weight * delta_percentile
            snapshot.update(
                {
                    "baseline": baseline,
                    "engagement_rate": engagement_rate,
                    "history_score": history_score,
                    "delta_percentile": delta_percentile,
                    "composite_score": composite,
                }
            )
            if composite >= self.composite_cutoff and delta_percentile >= self.delta_percentile_cutoff:
                viral.append(snapshot)
        return viral

    def _rank_percentile(self, sorted_values: List[float], value: float) -> float:
        if not sorted_values:
            return 0.0
        if sorted_values[-1] <= 0:
            return 0.0
        below_or_equal = len([item for item in sorted_values if item <= value])
        return below_or_equal / len(sorted_values)

    def _save_viral_hits(self, interval: int, hits: List[Dict]) -> None:
        if not hits:
            return

        timestamp = self._now_jalali_filename()
        by_account: Dict[str, List[Dict]] = defaultdict(list)
        for hit in hits:
            if hit["id"] not in self.viral_ids:
                by_account[hit["account"]].append(hit)

        for account, account_hits in by_account.items():
            folder = self.viral_root / self._interval_label(interval) / account.upper()
            folder.mkdir(parents=True, exist_ok=True)
            file_path = folder / f"{timestamp} VIRAL.txt"
            content = "\n".join(
                self._format_viral_hit(hit, interval, timestamp) + SNAPSHOT_SEPARATOR
                for hit in account_hits
            )
            file_path.write_text(content, encoding="utf-8")
            for hit in account_hits:
                self.viral_ids.add(hit["id"])
                print(f"  FIRE @{hit['account']}: {hit['id']} score={hit['composite_score']:.2f}")

    def _load_existing_viral_ids(self) -> Set[str]:
        viral_ids: Set[str] = set()
        for file_path in self.viral_root.glob("*/*/* VIRAL.txt"):
            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue
            for match in re.finditer(r"Tweet ID:\s*(\d+)", content):
                viral_ids.add(match.group(1))
        return viral_ids

    def _format_viral_hit(self, hit: Dict, interval: int, timestamp: str) -> str:
        tweet = hit["tweet"]
        metrics = hit["metrics"]
        baseline = hit["baseline"]
        lines = [
            "VIRAL TWEET",
            "",
            f"Account: @{hit['account']}",
            f"Interval: {self._interval_label(interval)}",
            f"Detected: {timestamp}",
            f"Tweet ID: {hit['id']}",
            f"Created: {tweet.get('jalali_time', '')}",
            f"Type: {tweet.get('type', 'tweet')}",
            "",
            f"Composite Score: {hit['composite_score']:.4f}",
            f"History Score: {hit['history_score']:.4f}",
            f"Delta Percentile: {hit['delta_percentile']:.4f}",
            f"Engagement Rate: {hit['engagement_rate']:.6f}",
            f"Historical p{self.threshold_percentile}: {baseline.get(f'p{self.threshold_percentile}', 0):.6f}",
            f"Weighted Delta: {hit['delta']:.2f}",
            "",
            self._wrap(tweet.get("text", ""), width=90),
            "",
            (
                f"Metrics: replies={metrics['replies']} retweets={metrics['retweets']} "
                f"likes={metrics['likes']} quotes={metrics['quotes']} "
                f"bookmarks={metrics['bookmarks']} views={metrics['views']}"
            ),
            f"Link: {tweet.get('link') or 'https://x.com/i/status/' + hit['id']}",
            "",
        ]
        return "\n".join(lines)

    def _run_due_viral_jobs(self) -> None:
        now = time.time()
        for interval in self.intervals_minutes:
            if now < self.next_viral_due.get(interval, 0):
                continue
            print(f"\n[Viral check] {self._interval_label(interval)}")
            refreshed = self._refresh_candidates(interval)
            hits = self._score_refreshed(refreshed)
            self._save_viral_hits(interval, hits)
            print(f"  Checked {len(refreshed)} candidate(s), viral hits: {len(hits)}")
            self.next_viral_due[interval] = now + interval * 60

    def _adaptive_live_delay(self, active_count: int) -> float:
        headrooms = [
            self._endpoint_headroom("UserTweets", default=10),
            self._endpoint_headroom("UserTweetsAndReplies", default=10),
        ]
        low_headroom = min(headrooms)
        if low_headroom <= max(active_count, 1):
            return MAX_LIVE_POLL_SECONDS
        if low_headroom < max(active_count * 3, 3):
            return min(MAX_LIVE_POLL_SECONDS, LIVE_POLL_SECONDS * 1.5)
        return max(MIN_LIVE_POLL_SECONDS, LIVE_POLL_SECONDS)

    def cleanup_old_candidate_files(self) -> None:
        cutoff = datetime.now(self.tz) - timedelta(days=self.window_days)
        for path in self.viral_root.glob("*/*/*.txt"):
            snapshot_time = self._parse_jalali_filename(path.stem.replace(" VIRAL", ""))
            if snapshot_time and snapshot_time < cutoff:
                try:
                    path.unlink()
                except Exception:
                    pass

    def run(self):
        active_accounts = list(ACCOUNTS)
        jitter = 10

        print("=" * 70)
        print("Twitter Live Monitor + Viral Detection")
        print("=" * 70)
        print(f"Monitoring: {', '.join(active_accounts)}")
        print(f"Viral intervals: {', '.join(self._interval_label(i) for i in self.intervals_minutes)}")
        print(f"Candidate root: {self.viral_root}")
        print("Press Ctrl+C to stop\n")

        while active_accounts:
            self.cleanup_old_candidate_files()
            self._run_due_viral_jobs()

            for username in list(active_accounts):
                if self.errors.get(username, 0) >= MAX_CONSECUTIVE_ERRORS:
                    print(f"  x Too many errors for @{username}; removing from active monitor")
                    active_accounts.remove(username)
                    continue

                try:
                    self.poll_account_once(username)
                except Exception as exc:
                    self.errors[username] = self.errors.get(username, 0) + 1
                    print(
                        f"  ! Error for @{username} "
                        f"({self.errors[username]}/{MAX_CONSECUTIVE_ERRORS}): {exc}"
                    )

                delay = self._adaptive_live_delay(len(active_accounts)) / max(len(active_accounts), 1)
                time.sleep(max(5, delay + random.uniform(-jitter, jitter)))


def main():
    os.chdir(Path(__file__).parent)

    if not ACCOUNTS:
        print("No accounts configured. Edit ACCOUNTS at top of script.")
        sys.exit(1)

    monitor = TwitterLiveMonitor()
    try:
        monitor.run()
    except KeyboardInterrupt:
        print("\nStopped by user")


if __name__ == "__main__":
    main()
