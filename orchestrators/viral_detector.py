#!/usr/bin/env python3
"""
V4 live viral detection using isolated live snapshots.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from orchestrators.live_storage import LiveStorageManager


class ViralDetector:
    """Detect viral candidates from engagement velocity and acceleration."""

    def __init__(self, config_path: str = "config/config.json", storage: Optional[LiveStorageManager] = None):
        self.project_root = Path(__file__).resolve().parent
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
            path = self.project_root / "data" / "processed" / folder / key / f"{folder}.json"
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
