#!/usr/bin/env python3
"""
Twitter Live Tweet Monitor - Hybrid

Uses api_manager.py and storage_manager.py for clean separation of concerns.

Key improvements:
- NEVER deep crawls (priority-based rolling window)
- Incremental delta tracking
- Snapshot storage for viral detection
- Adaptive polling based on account tier
- Global dedupe registry
- Graceful degradation

CRITICAL: This script only fetches NEW tweets, not historical data.
"""

import json
import math
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Import new managers
from api_manager import APIManager
from storage_manager import StorageManager
from storage_manager import extract_metrics
from tier_config import get_priority_policy, load_tier_config, ordered_accounts
from viral_detector import ViralDetector

# Import the historical fetcher for tweet parsing logic
from fetch_historical_tweets_hybrid import TwitterHistoricalFetcher

try:
    import jdatetime
    import pytz
except ImportError:
    print("ERROR: Missing dependencies. Run: pip3 install jdatetime pytz")
    sys.exit(1)

# ============================================================================
# CONFIGURATION
# ============================================================================

# Default polling fallback for unknown accounts.
DEFAULT_POLL_INTERVAL_SECONDS = 1440
# Fallback live window if policy is unavailable.
LIVE_WINDOW_HOURS = 24

# Maximum consecutive errors before skipping account
MAX_CONSECUTIVE_ERRORS = 5

# Timezone
TIMEZONE = "Asia/Tehran"

SEP = "═" * 70


# ============================================================================
# LIVE MONITOR CLASS
# ============================================================================

class TwitterLiveMonitor:
    """
    Live tweet monitor with incremental delta tracking
    
    CRITICAL DIFFERENCES FROM HISTORICAL FETCHER:
    - NEVER deep crawls (max 1-2 pages)
    - Fetches tweets from priority-based rolling windows
    - Tracks deltas using dedupe registry
    - Saves snapshots for viral detection
    - Adaptive polling based on tier
    """
    
    def __init__(self, config_path: str = "config.json"):
        self.base_dir = Path(__file__).parent
        
        # Initialize managers
        print("Initializing live monitor...")
        self.api_manager = APIManager(config_path, state_dir=self.base_dir / "data" / "STATE")
        self.storage_manager = StorageManager(self.base_dir, timezone=TIMEZONE)
        
        self.viral_detector = ViralDetector(config_path)
        # Use historical fetcher for parsing logic (DRY principle)
        self.fetcher = TwitterHistoricalFetcher(config_path)
        self.fetcher.api_manager = self.api_manager
        self.fetcher.storage_manager = self.storage_manager
        
        self.config = self.api_manager.config
        self.tz = pytz.timezone(TIMEZONE)
        
        # Tier and policy architecture shared with historical mode.
        self.account_map, self.priority_policies = load_tier_config(self.config)
        self.accounts = ordered_accounts(self.account_map)
        
        # Track last fetch time per account
        self.last_fetch: Dict[str, datetime] = {}
        
        # Track consecutive errors per account
        self.error_count: Dict[str, int] = defaultdict(int)
        
        print("✓ Live monitor initialized")
        print(f"  - Monitoring {len(self.accounts)} accounts")

    def get_priority_policy(self, username: str) -> Dict:
        """Return per-account policy (interval + windows + page limits)."""
        return get_priority_policy(username, self.account_map, self.priority_policies)

    def _random_human_pause(self, bucket: str = "between_accounts"):
        """Random pause using anti-bot simulation config."""
        sim = self.config.get("anti_bot_simulation", {})
        if not sim.get("enabled", True):
            return

        delay_cfg = sim.get("delays_seconds", {})
        if bucket == "between_cycles":
            min_d = float(delay_cfg.get("between_cycles_min", 45))
            max_d = float(delay_cfg.get("between_cycles_max", 120))
        else:
            min_d = float(delay_cfg.get("between_accounts_min", 2))
            max_d = float(delay_cfg.get("between_accounts_max", 5))

        if max_d < min_d:
            max_d = min_d
        time.sleep(random.uniform(min_d, max_d))
    
    def _is_within_live_window(self, timestamp_str: str, live_window_hours: int) -> bool:
        """Check if tweet is within live monitoring window"""
        try:
            parts = timestamp_str.split()
            if len(parts) != 2:
                return True
            
            date_parts = parts[0].split('-')
            time_parts = parts[1].split(':')
            
            if len(date_parts) != 3 or len(time_parts) != 3:
                return True
            
            jalali_dt = jdatetime.datetime(
                int(date_parts[0]),
                int(date_parts[1]),
                int(date_parts[2]),
                int(time_parts[0]),
                int(time_parts[1]),
                int(time_parts[2])
            )
            
            gregorian_dt = jalali_dt.togregorian()
            dt_aware = self.tz.localize(gregorian_dt)
            
            cutoff = datetime.now(self.tz) - timedelta(hours=live_window_hours)
            return dt_aware >= cutoff
        except:
            return True
    
    def should_fetch_account(self, username: str) -> bool:
        """Check if it's time to fetch this account based on tier"""
        policy = self.get_priority_policy(username)
        interval = int(policy.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS))
        
        last_fetch = self.last_fetch.get(username)
        if not last_fetch:
            return True
        
        elapsed = (datetime.now(self.tz) - last_fetch).total_seconds()
        return elapsed >= interval
    
    def fetch_latest_tweets(
        self,
        username: str,
        user_id: str,
        max_pages: int = 2,
        live_window_hours: int = LIVE_WINDOW_HOURS,
    ) -> Tuple[List[Dict], List[Dict], List[Dict], List[Dict]]:
        """
        Fetch only the latest tweets (NEVER deep crawl)
        
        CRITICAL: max_pages is limited to 1-2 for live monitoring
        """
        print(f"  📄 Fetching latest tweets (max {max_pages} pages)...")
        
        tweets = self.fetcher.fetch_user_tweets(user_id, max_pages=max_pages, username=username)
        replies = self.fetcher.fetch_user_tweets_and_replies(user_id, username, max_pages=max_pages)
        only, replies_only, merged, diffs = self.storage_manager.compare_endpoints(tweets, replies)

        datasets = []
        for items in [only, replies_only, merged, diffs]:
            filtered = [
                t for t in items
                if self.fetcher._is_account_timeline_item(t, username)
                and self._is_within_live_window(t.get("timestamp", ""), live_window_hours=live_window_hours)
            ]
            datasets.append(filtered)

        print(f"  ✓ Found {len(datasets[2])} merged tweets in live window")
        return datasets[0], datasets[1], datasets[2], datasets[3]
    
    def process_new_tweets(self, username: str, tweets: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """
        Process tweets and separate into new vs existing
        
        Returns:
            (new_tweets, existing_tweets)
        """
        new_tweets = []
        existing_tweets = []
        
        for tweet in tweets:
            tweet_id = tweet.get("id")
            if not tweet_id:
                continue
            
            if self.storage_manager.is_tweet_seen(tweet_id):
                existing_tweets.append(tweet)
            else:
                new_tweets.append(tweet)
        
        return new_tweets, existing_tweets
    
    def save_snapshots(self, tweets: List[Dict]):
        """Save engagement snapshots for viral detection"""
        viral_detected = []
        
        for tweet in tweets:
            tweet_id = tweet.get("id")
            if not tweet_id:
                continue
            
            metrics = {
                "likes": tweet.get("likes"),
                "retweets": tweet.get("retweets"),
                "replies": tweet.get("replies"),
                "views": tweet.get("views"),
                "bookmarks": tweet.get("bookmarks"),
                "quotes": tweet.get("quotes"),
            }

            # If raw object is available in future extensions, centralized parser stays reusable.
            if "raw_tweet_obj" in tweet and isinstance(tweet["raw_tweet_obj"], dict):
                parsed = extract_metrics(tweet["raw_tweet_obj"])
                metrics.update(parsed)
            
            if any(v is not None for v in metrics.values()):
                saved = self.storage_manager.save_snapshot(tweet_id, metrics, tweet=tweet)
                if saved:
                    print(f"  ✓ Saved snapshot for {tweet_id}")
                
                # Check for viral potential
                account = tweet.get("account", "UNKNOWN")
                viral_analysis = self.viral_detector.analyze_tweet(
                    tweet_id, account, tweet
                )
                
                if viral_analysis:
                    viral_detected.append(viral_analysis)
        
        # Save viral reports
        if viral_detected:
            print(f"\n  🔥 VIRAL DETECTED: {len(viral_detected)} tweet(s)")
            for analysis in viral_detected:
                self.storage_manager.save_viral_report(
                    tweet=analysis["tweet"],
                    metrics=analysis["metrics"],
                    velocity=analysis["velocity"],
                    classification=analysis["classification"],
                    confirmed=analysis["confirmed"]
                )
                account = analysis["account"]
                tweet_id = analysis["tweet_id"]
                print(f"     - @{account}: {tweet_id} ({analysis['classification']})")
    
    def monitor_account(self, username: str) -> bool:
        """Monitor a single account for new tweets"""
        policy = self.get_priority_policy(username)
        tier = int(policy.get("priority", 7))
        live_window_hours = int(policy.get("live_window_hours", LIVE_WINDOW_HOURS))
        max_pages = int(policy.get("live_max_pages", 2))
        
        print(
            f"  🔍 Monitoring @{username} (Tier {tier}) "
            f"| window={live_window_hours}h | max_pages={max_pages}"
        )
        self.api_manager.warmup_user_context(username)
        
        user_id = self.fetcher.get_user_id(username)
        if not user_id:
            self.error_count[username] += 1
            self.storage_manager.log_event("fetch_failures", f"Failed to resolve @{username}")
            return False
        
        try:
            tweets_only, replies_only, merged, diffs = self.fetch_latest_tweets(
                username,
                user_id,
                max_pages=max_pages,
                live_window_hours=live_window_hours,
            )
        except Exception as e:
            print(f"  ✗ Error fetching tweets: {e}")
            self.error_count[username] += 1
            self.storage_manager.log_event("fetch_failures", f"Error fetching @{username}: {e}")
            return False
        
        if not merged:
            print(f"  ℹ️  No tweets in live window")
            self.last_fetch[username] = datetime.now(self.tz)
            return True
        
        new_tweets, existing_tweets = self.process_new_tweets(username, merged)
        
        print(f"  📊 Found {len(new_tweets)} new, {len(existing_tweets)} existing")
        
        self.save_snapshots(merged)
        
        if new_tweets:
            new_ids = {tweet.get("id") for tweet in new_tweets if tweet.get("id")}
            data_sets = {
                "USER_TWEETS": [t for t in tweets_only if t.get("id") in new_ids],
                "USER_TWEETS_AND_REPLIES": [t for t in replies_only if t.get("id") in new_ids],
                "MERGED_TIMELINES": [t for t in merged if t.get("id") in new_ids],
                "ENDPOINT_DIFFS": [t for t in diffs if t.get("id") in new_ids],
            }

            folder_map = {
                "USER_TWEETS": self.storage_manager.user_tweets_dir,
                "USER_TWEETS_AND_REPLIES": self.storage_manager.user_replies_dir,
                "MERGED_TIMELINES": self.storage_manager.merged_dir,
                "ENDPOINT_DIFFS": self.storage_manager.endpoint_diffs_dir,
            }

            saved_count = 0
            for dataset_name, dataset_tweets in data_sets.items():
                tweets_by_date = defaultdict(list)
                for tweet in dataset_tweets:
                    try:
                        date_str = tweet["timestamp"].split()[0]
                    except Exception:
                        date_str = self.storage_manager.get_jalali_date()
                    tweets_by_date[date_str].append(tweet)

                for date_str, day_tweets in tweets_by_date.items():
                    count = self.storage_manager.save_tweets_to_file(
                        day_tweets,
                        username,
                        date_str,
                        folder_map[dataset_name],
                        append=True,
                    )
                    saved_count += count

            stored_lookup = defaultdict(list)
            for dataset_name, dataset_tweets in data_sets.items():
                for item in dataset_tweets:
                    tid = item.get("id")
                    if tid and dataset_name not in stored_lookup[tid]:
                        stored_lookup[tid].append(dataset_name)

            for tweet in new_tweets:
                tweet_id = tweet.get("id")
                if not tweet_id:
                    continue
                stored_in = stored_lookup.get(tweet_id, [])
                if not stored_in:
                    stored_in = ["MERGED_TIMELINES"]
                self.storage_manager.register_tweet(tweet_id, username, stored_in)
            
            print(f"  ✅ Saved {saved_count} new tweets")
            self.storage_manager.log_event("viral_events", f"@{username}: {saved_count} new tweets")
        else:
            print(f"  ℹ️  No new tweets to save")
        
        self.last_fetch[username] = datetime.now(self.tz)
        self.error_count[username] = 0
        
        return True
    
    def run_monitoring_cycle(self):
        """Run one monitoring cycle for all accounts"""
        print(f"\n{SEP}")
        print(f"MONITORING CYCLE - {self.storage_manager.get_jalali_datetime()}")
        print(f"{SEP}\n")
        
        accounts_to_check = []
        
        for username in self.accounts:
            tier = int(self.get_priority_policy(username).get("priority", 7))
            if self.error_count[username] >= MAX_CONSECUTIVE_ERRORS:
                print(f"⏭️  Skipping @{username} (too many errors)")
                continue
            
            if self.should_fetch_account(username):
                accounts_to_check.append((username, tier))
        
        if not accounts_to_check:
            print("ℹ️  No accounts due for checking this cycle")
            return
        
        accounts_to_check.sort(key=lambda x: x[1])
        
        print(f"📋 Checking {len(accounts_to_check)} accounts this cycle\n")
        
        successful = 0
        failed = 0
        
        for i, (username, tier) in enumerate(accounts_to_check, 1):
            print(f"[{i}/{len(accounts_to_check)}] @{username}")
            print("-" * 70)
            
            try:
                ok = self.monitor_account(username)
                if ok:
                    successful += 1
                else:
                    failed += 1
            except Exception as e:
                print(f"  ✗ Fatal error: {e}")
                self.storage_manager.log_event("fetch_failures", f"Fatal error for @{username}: {e}")
                failed += 1
            
            if i < len(accounts_to_check):
                self._random_human_pause("between_accounts")
        
        print(f"\n{SEP}")
        print(f"CYCLE COMPLETE")
        print(f"{SEP}")
        print(f"✅ Successful: {successful}")
        print(f"⚠️  Failed: {failed}")
        
        stats = self.api_manager.get_stats()
        print(f"\n📊 Session Statistics:")
        print(f"   - Requests made: {stats['requests_made']}")
        print(f"   - Requests per minute: {stats['requests_per_minute']}")
    
    def run_continuous(self, check_interval: int = 60):
        """Run continuous monitoring"""
        print(f"\n{SEP}")
        print(f"STARTING CONTINUOUS MONITORING")
        print(f"{SEP}")
        print(f"\nMonitoring {len(self.accounts)} accounts:")
        for priority in sorted(self.priority_policies.keys()):
            accounts = [a for a in self.accounts if int(self.get_priority_policy(a)["priority"]) == priority]
            if not accounts:
                continue
            interval = int(self.priority_policies[priority]["poll_interval_seconds"])
            checks_per_5m = 300.0 / float(max(interval, 1))
            print(
                f"  - Priority {priority} ({len(accounts)} accounts): "
                f"every {interval//60}m {interval%60}s "
                f"(~{checks_per_5m:.2f} checks/account/5m)"
            )
        print(f"\nPress Ctrl+C to stop\n")
        
        cycle_count = 0
        
        try:
            while True:
                cycle_count += 1
                self.run_monitoring_cycle()
                extra_pause = 0
                sim = self.config.get("anti_bot_simulation", {})
                if sim.get("enabled", True):
                    delay_cfg = sim.get("delays_seconds", {})
                    min_d = float(delay_cfg.get("between_cycles_min", 0))
                    max_d = float(delay_cfg.get("between_cycles_max", 0))
                    if max_d < min_d:
                        max_d = min_d
                    extra_pause = random.uniform(min_d, max_d) if max_d > 0 else 0

                total_wait = check_interval + int(extra_pause)
                print(f"\n⏸️  Waiting {total_wait}s before next cycle check...")
                time.sleep(total_wait)
                
        except KeyboardInterrupt:
            print(f"\n\n{SEP}")
            print(f"MONITORING STOPPED")
            print(f"{SEP}")
            print(f"\n📊 Final Statistics:")
            print(f"   - Cycles completed: {cycle_count}")
            
            stats = self.api_manager.get_stats()
            print(f"   - Total requests: {stats['requests_made']}")
            print(f"   - Session duration: {stats['session_duration_seconds']}s")
            
            print(f"\n✅ Monitoring stopped gracefully")


def main():
    print(SEP)
    print("Twitter Live Tweet Monitor - Hybrid")
    print(SEP)
    print("\nUsing new architecture:")
    print("  - api_manager.py for networking")
    print("  - storage_manager.py for storage")
    print("  - Incremental delta tracking")
    print("  - Snapshot storage for viral detection")
    print("  - Adaptive polling based on tier")
    print("  - NEVER deep crawls (priority rolling windows)\n")
    
    monitor = TwitterLiveMonitor()
    monitor.run_continuous(check_interval=60)


if __name__ == "__main__":
    main()
