#!/usr/bin/env python3
"""
Storage manager for Phase 3 raw/processed persistence.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


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
    ):
        # `base_dir`/`timezone` kept for backward compatibility with Phase-2 engine.
        _ = timezone
        self.project_root = project_root or base_dir or Path(__file__).resolve().parent.parent
        self.data_root = self.project_root / "data"
        self.raw_root = self.data_root / "raw_json"
        self.processed_root = self.data_root / "processed"
        self.state_dir = self.data_root / "state"
        self.legacy_state_dir = self.data_root / "STATE"
        self.sync_state_file = self.state_dir / "sync_state.json"
        self.legacy_sync_state_file = self.legacy_state_dir / "sync_state.json"

        # Compatibility aliases used by existing fetching code.
        self.raw_user_tweets_dir = self.raw_root / "UserTweets"
        self.raw_user_replies_dir = self.raw_root / "UserTweetsAndReplies"
        self.user_tweets_dir = self.processed_root / "1_user_tweets"
        self.user_replies_dir = self.processed_root / "2_user_tweets_and_replies"
        self.intersection_dir = self.processed_root / "3_intersection"
        self.merged_dir = self.processed_root / "4_union"
        self.endpoint_diffs_dir = self.processed_root / "5_replies_only"
        self.logs_dir = self.project_root / "logs"

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
            self.legacy_state_dir,
            self.logs_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

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
        """Load sync state from canonical path, fallback to legacy STATE path."""
        data = self._read_json_file(self.sync_state_file)
        if data:
            return data
        return self._read_json_file(self.legacy_sync_state_file)

    def save_sync_state(self, state: Dict[str, Any]) -> Path:
        """Persist sync state to canonical + legacy path for compatibility."""
        payload = state if isinstance(state, dict) else {}
        self.sync_state_file.parent.mkdir(parents=True, exist_ok=True)
        with self.sync_state_file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        self.legacy_sync_state_file.parent.mkdir(parents=True, exist_ok=True)
        with self.legacy_sync_state_file.open("w", encoding="utf-8") as f:
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
        """Save processed set output as formatted JSON in mapped set folder."""
        normalized = str(set_name).strip()
        folder = self.SET_FOLDER_MAP.get(normalized, self.SET_FOLDER_MAP.get(normalized.upper()))
        if not folder:
            raise ValueError(f"Unsupported set_name: {set_name}")

        target_dir = self.processed_root / folder / self._normalize_username(username)
        target_dir.mkdir(parents=True, exist_ok=True)
        output_file = target_dir / f"{folder}.json"

        with output_file.open("w", encoding="utf-8") as f:
            json.dump(data_list if isinstance(data_list, list) else [], f, ensure_ascii=False, indent=2)
        return output_file

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
        """Compatibility date helper (Gregorian fallback)."""
        target = dt or datetime.utcnow()
        return target.strftime("%Y-%m-%d")

    @staticmethod
    def get_jalali_datetime(dt: Optional[datetime] = None) -> str:
        """Compatibility datetime helper (Gregorian fallback)."""
        target = dt or datetime.utcnow()
        return target.strftime("%Y-%m-%d %H:%M:%S")
