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

from src.shared.data_pipeline.storage_manager import StorageManager, extract_metrics


class LiveStorageManager:
    """Keep live state and outputs separate from historical sync state."""

    def __init__(
        self,
        project_root: Optional[Path] = None,
        timezone: str = "Asia/Tehran",
        data_root_override: Optional[Path] = None,
    ):
        # تغییر parent به parents[4] برای رسیدن به ریشه پروژه
        self.project_root = project_root or Path(__file__).resolve().parents[4]
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

    def save_processed_set(self, username: str, set_name: str, tweets: List[Dict[str, Any]]) -> List[Path]:
        # Merge into the shared historical_live store (same writer historical uses),
        # producing {folder}.json (merged by tweet id) + per-Jalali-date .txt files.
        # This unifies live with historical so a live run accumulates instead of
        # overwriting the previously-merged historical set.
        return self.storage.save_processed_set_merged(tweets or [], set_name, username)

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


#!/usr/bin/env python3
"""
V4 live viral detection using isolated live snapshots.
"""


import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# LiveStorageManager is defined in this file, no import needed


class ViralDetector:
    """Detect viral candidates from engagement velocity and acceleration."""

    def __init__(self, config_path: str = "src/shared/config/config.json", storage: Optional[LiveStorageManager] = None):
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
