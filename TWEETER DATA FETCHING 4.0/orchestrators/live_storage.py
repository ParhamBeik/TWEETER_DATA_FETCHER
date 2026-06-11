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

from data_pipeline.storage_manager import StorageManager, extract_metrics


class LiveStorageManager:
    """Keep live state and outputs separate from historical sync state."""

    def __init__(self, project_root: Optional[Path] = None, timezone: str = "Asia/Tehran"):
        self.project_root = project_root or Path(__file__).resolve().parent
        self.storage = StorageManager(base_dir=self.project_root, timezone=timezone, subsystem="live")
        self.data_root = self.project_root / "data" / "live"
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
