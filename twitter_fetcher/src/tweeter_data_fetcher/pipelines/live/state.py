#!/usr/bin/env python3
from __future__ import annotations
"""
Isolated v4 live-monitoring storage and viral-report helpers.
"""


import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tweeter_data_fetcher.paths import PROJECT_ROOT
from tweeter_data_fetcher.storage.facade import StorageManager


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

    def save_processed_set(self, username: str, set_name: str, tweets: List[Dict[str, Any]]) -> List[Path]:
        # Merge into the shared historical_live store (same writer historical uses),
        # producing {folder}.json (merged by tweet id) + per-Jalali-date .txt files.
        # This unifies live with historical so a live run accumulates instead of
        # overwriting the previously-merged historical set.
        return self.storage.save_processed_set_merged(tweets or [], set_name, username)
