#!/usr/bin/env python3
"""
Viral Tweet Detection Engine

Analyzes engagement snapshots to detect viral tweets based on:
- Velocity (engagement per minute)
- Acceleration (change in velocity)
- Per-account baseline comparison
- Composite scoring

Uses the snapshot system from storage_manager.py
"""

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import jdatetime
    import pytz
except ImportError:
    print("ERROR: Missing dependencies. Run: pip3 install jdatetime pytz")
    import sys
    sys.exit(1)


class ViralDetector:
    """
    Detects viral tweets by analyzing engagement velocity and acceleration
    """
    
    def __init__(self, config_path: str = "config.json"):
        """Initialize viral detector with config"""
        self.base_dir = Path(__file__).parent
        cfg_path = Path(config_path)
        if not cfg_path.is_absolute():
            cfg_path = self.base_dir / cfg_path
        self.config_path = cfg_path
        self.config = self._load_config(str(cfg_path))
        self.viral_config = self.config.get("viral_detection", self.config.get("viral_config", {}))
        
        # Viral detection parameters
        self.window_days = self.viral_config.get("window_days", 7)
        self.threshold_percentile = self.viral_config.get("threshold_percentile", 95)
        self.history_weight = self.viral_config.get("history_score_weight", 0.3)
        self.delta_weight = self.viral_config.get("delta_score_weight", 0.7)
        self.composite_cutoff = self.viral_config.get("composite_score_cutoff", 1.0)
        self.delta_percentile_cutoff = self.viral_config.get("delta_percentile_cutoff", 0.8)
        
        # Paths
        self.data_dir = self.base_dir / "data"
        self.snapshots_dir = self.data_dir / "SNAPSHOTS"
        self.user_tweets_dir = self.data_dir / "USER_TWEETS"
        self.snapshot_index_file = self.data_dir / "STATE" / "snapshot_index.json"
        
        # Timezone
        self.tz = pytz.timezone("Asia/Tehran")
        
        # Cache for account baselines
        self.account_baselines = {}

    def _load_snapshot_index(self) -> Dict[str, str]:
        if self.snapshot_index_file.exists():
            try:
                with open(self.snapshot_index_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
            except Exception:
                pass
        return {}

    def _locate_snapshot_file(self, tweet_id: str) -> Optional[Path]:
        """Find new nested snapshot paths first, then legacy flat files."""
        index = self._load_snapshot_index()
        rel = index.get(tweet_id)
        if rel:
            candidate = self.data_dir / rel
            if candidate.exists():
                return candidate

        legacy = self.snapshots_dir / f"{tweet_id}.json"
        if legacy.exists():
            return legacy

        for nested in self.snapshots_dir.glob(f"*/*_{tweet_id}.json"):
            return nested

        return None
    
    def _load_config(self, config_path: str) -> Dict:
        """Load configuration"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️  Could not load config: {e}")
            return {}
    
    def load_snapshots(self, tweet_id: str) -> List[Dict]:
        """
        Load all snapshots for a tweet
        
        Returns:
            List of snapshots sorted by timestamp (oldest first)
        """
        snapshot_file = self._locate_snapshot_file(tweet_id)
        if not snapshot_file or not snapshot_file.exists():
            return []
        
        try:
            with open(snapshot_file, 'r', encoding='utf-8') as f:
                snapshots = json.load(f)
            
            # Sort by timestamp
            snapshots.sort(key=lambda x: x.get("timestamp", ""))
            return snapshots
        except Exception as e:
            print(f"⚠️  Error loading snapshots for {tweet_id}: {e}")
            return []
    
    def calculate_velocity(self, snapshots: List[Dict]) -> Optional[Dict]:
        """
        Calculate engagement velocity (per minute) from snapshots
        
        Returns:
            Dict with velocity metrics or None if insufficient data
        """
        if len(snapshots) < 2:
            return None
        
        first = snapshots[0]
        last = snapshots[-1]
        
        # Parse timestamps
        try:
            t1 = datetime.fromisoformat(first["timestamp"])
            t2 = datetime.fromisoformat(last["timestamp"])
            time_delta_minutes = (t2 - t1).total_seconds() / 60.0
            
            if time_delta_minutes <= 0:
                return None
        except Exception as e:
            print(f"⚠️  Error parsing timestamps: {e}")
            return None
        
        # Calculate deltas
        velocity = {}
        metrics = ["likes", "retweets", "replies", "views", "bookmarks", "quotes"]
        
        for metric in metrics:
            val1 = first.get(metric)
            val2 = last.get(metric)
            
            if val1 is not None and val2 is not None:
                delta = val2 - val1
                velocity[f"{metric}_per_min"] = delta / time_delta_minutes
            else:
                velocity[f"{metric}_per_min"] = None
        
        velocity["time_window_minutes"] = time_delta_minutes
        velocity["snapshot_count"] = len(snapshots)
        
        return velocity

    def calculate_multi_window_velocity(self, snapshots: List[Dict]) -> Optional[Dict]:
        """Calculate per-minute velocity over multiple windows (5m/30m/120m)."""
        if len(snapshots) < 2:
            return None

        parsed = []
        for snap in snapshots:
            try:
                parsed.append((datetime.fromisoformat(snap["timestamp"]), snap))
            except Exception:
                continue

        if len(parsed) < 2:
            return None

        parsed.sort(key=lambda item: item[0])
        latest_ts, latest = parsed[-1]
        windows = [5, 30, 120]
        metrics = ["likes", "retweets", "replies", "views", "quotes", "bookmarks"]
        results = {}

        for window in windows:
            start = None
            for ts, snap in reversed(parsed):
                delta_minutes = (latest_ts - ts).total_seconds() / 60.0
                if delta_minutes >= window:
                    start = snap
                    break
            if not start:
                continue

            for metric in metrics:
                v1 = start.get(metric, 0)
                v2 = latest.get(metric, 0)
                try:
                    v1 = 0.0 if v1 in [None, "unknown"] else float(v1)
                    v2 = 0.0 if v2 in [None, "unknown"] else float(v2)
                except Exception:
                    v1 = 0.0
                    v2 = 0.0
                results[f"{metric}_per_min_{window}m"] = (v2 - v1) / float(window)

        return results if results else None

    def calculate_engagement_quality(self, metrics: Dict) -> float:
        """Engagement quality = (likes+retweets+replies+quotes)/views."""
        def _to_num(v):
            if v in [None, "unknown"]:
                return 0.0
            try:
                return float(v)
            except Exception:
                return 0.0

        views = max(_to_num(metrics.get("views", 0)), 1.0)
        engagement = (
            _to_num(metrics.get("likes", 0)) +
            _to_num(metrics.get("retweets", 0)) +
            _to_num(metrics.get("replies", 0)) +
            _to_num(metrics.get("quotes", 0))
        )
        return engagement / views

    def calculate_momentum(self, snapshots: List[Dict]) -> float:
        """Rolling momentum from the recent likes slope."""
        if len(snapshots) < 4:
            return 0.0

        recent = snapshots[-5:]
        likes = []
        for snap in recent:
            val = snap.get("likes", 0)
            if val in [None, "unknown"]:
                val = 0
            try:
                likes.append(float(val))
            except Exception:
                likes.append(0.0)

        diffs = []
        for idx in range(1, len(likes)):
            diffs.append(likes[idx] - likes[idx - 1])
        if len(diffs) < 2:
            return 0.0

        return diffs[-1] - diffs[0]
    
    def calculate_acceleration(self, snapshots: List[Dict]) -> Optional[Dict]:
        """
        Calculate engagement acceleration (change in velocity)
        
        Returns:
            Dict with acceleration metrics or None if insufficient data
        """
        if len(snapshots) < 3:
            return None
        
        # Split into two halves
        mid = len(snapshots) // 2
        first_half = snapshots[:mid+1]
        second_half = snapshots[mid:]
        
        # Calculate velocity for each half
        vel1 = self.calculate_velocity(first_half)
        vel2 = self.calculate_velocity(second_half)
        
        if not vel1 or not vel2:
            return None
        
        # Calculate acceleration
        acceleration = {}
        metrics = ["likes", "retweets", "replies", "views", "bookmarks", "quotes"]
        
        for metric in metrics:
            v1 = vel1.get(f"{metric}_per_min")
            v2 = vel2.get(f"{metric}_per_min")
            
            if v1 is not None and v2 is not None:
                acceleration[f"{metric}_acceleration"] = v2 - v1
            else:
                acceleration[f"{metric}_acceleration"] = None
        
        return acceleration
    
    def get_account_baseline(self, account: str) -> Dict:
        """
        Calculate baseline engagement metrics for an account from historical data
        
        Returns:
            Dict with percentile values for each metric
        """
        # Check cache
        if account in self.account_baselines:
            return self.account_baselines[account]
        
        # Load historical tweets
        account_dir = self.user_tweets_dir / str(account).upper()

        if not account_dir.exists():
            return {}
        
        # Collect all engagement metrics from historical tweets
        all_metrics = defaultdict(list)
        
        for tweet_file in account_dir.glob("*.txt"):
            try:
                with open(tweet_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                # Parse metrics from tweet files
                for line in content.split('\n'):
                    normalized = line.strip().lower()
                    if "likes:" in normalized:
                        val = self._parse_metric_value(line)
                        if val is not None:
                            all_metrics['likes'].append(val)
                    elif "retweets:" in normalized:
                        val = self._parse_metric_value(line)
                        if val is not None:
                            all_metrics['retweets'].append(val)
                    elif "views:" in normalized:
                        val = self._parse_metric_value(line)
                        if val is not None:
                            all_metrics['views'].append(val)
            except Exception:
                continue
        
        # Calculate percentiles
        baseline = {}
        for metric, values in all_metrics.items():
            if values:
                values.sort()
                percentile_idx = int(len(values) * (self.threshold_percentile / 100.0))
                baseline[f"{metric}_p{self.threshold_percentile}"] = values[percentile_idx]
        
        # Cache result
        self.account_baselines[account] = baseline
        return baseline
    
    def _parse_metric_value(self, line: str) -> Optional[int]:
        """Parse metric value from a line like 'Likes: 1,234' or 'Likes: unknown'"""
        try:
            parts = line.split(':', 1)
            if len(parts) != 2:
                return None

            value_str = parts[1].strip()
            if "|" in value_str:
                value_str = value_str.split("|", 1)[0].strip()

            if value_str.lower() in ['unknown', 'none', '']:
                return None

            digits_only = ''.join(ch for ch in value_str if ch.isdigit())
            if not digits_only:
                return None
            return int(digits_only)
        except Exception:
            pass
        return None
    
    def classify_viral(
        self,
        tweet_id: str,
        account: str,
        current_metrics: Dict,
        velocity: Dict,
        acceleration: Optional[Dict] = None,
        snapshots: Optional[List[Dict]] = None
    ) -> Tuple[bool, str, float]:
        """
        Classify if a tweet is viral
        
        Returns:
            (is_viral, classification_label, composite_score)
        """
        baseline = self.get_account_baseline(account)
        
        if not baseline:
            # No baseline available, use absolute thresholds
            likes = current_metrics.get("likes", 0) or 0
            views = current_metrics.get("views", 0) or 0
            
            if likes > 10000 and views > 1000000:
                return True, "HIGH_ABSOLUTE_ENGAGEMENT", 2.0
            elif likes > 5000 and views > 500000:
                return True, "MODERATE_ABSOLUTE_ENGAGEMENT", 1.5
            else:
                return False, "NORMAL", 0.5
        
        # Multi-window velocity blend (prefer short-term breakout with longer confirmation)
        multi_velocity = self.calculate_multi_window_velocity(snapshots or [])
        if multi_velocity:
            velocity = dict(velocity)
            velocity["likes_per_min"] = (
                multi_velocity.get("likes_per_min_5m", 0) * 0.5 +
                multi_velocity.get("likes_per_min_30m", 0) * 0.3 +
                multi_velocity.get("likes_per_min_120m", 0) * 0.2
            )
            velocity["views_per_min"] = (
                multi_velocity.get("views_per_min_5m", 0) * 0.5 +
                multi_velocity.get("views_per_min_30m", 0) * 0.3 +
                multi_velocity.get("views_per_min_120m", 0) * 0.2
            )
            velocity["retweets_per_min"] = (
                multi_velocity.get("retweets_per_min_5m", 0) * 0.5 +
                multi_velocity.get("retweets_per_min_30m", 0) * 0.3 +
                multi_velocity.get("retweets_per_min_120m", 0) * 0.2
            )

        # Calculate score based on velocity vs baseline
        score = 0.0
        
        # Likes velocity score
        likes_vel = velocity.get("likes_per_min", 0) or 0
        likes_baseline = baseline.get(f"likes_p{self.threshold_percentile}", 1)
        
        if likes_baseline > 0:
            likes_ratio = likes_vel / (likes_baseline / 1440)  # baseline per day -> per minute
            score += likes_ratio * 0.4
        
        # Views velocity score
        views_vel = velocity.get("views_per_min", 0) or 0
        views_baseline = baseline.get(f"views_p{self.threshold_percentile}", 1)
        
        if views_baseline > 0:
            views_ratio = views_vel / (views_baseline / 1440)
            score += views_ratio * 0.3
        
        # Retweets velocity score
        rt_vel = velocity.get("retweets_per_min", 0) or 0
        rt_baseline = baseline.get(f"retweets_p{self.threshold_percentile}", 1)
        
        if rt_baseline > 0:
            rt_ratio = rt_vel / (rt_baseline / 1440)
            score += rt_ratio * 0.3

        # Engagement quality bonus/penalty
        eng_rate = self.calculate_engagement_quality(current_metrics)
        if eng_rate > 0.08:
            score += 0.8
        elif eng_rate > 0.05:
            score += 0.4
        elif eng_rate < 0.01:
            score -= 0.5

        # Momentum bonus/penalty
        momentum = self.calculate_momentum(snapshots or [])
        if momentum > 50:
            score += 1.0
        elif momentum > 10:
            score += 0.5
        elif momentum < -10:
            score -= 0.5

        # Spread ratio bonus
        try:
            likes_now = 0.0 if current_metrics.get("likes") in [None, "unknown"] else float(current_metrics.get("likes", 0))
            rts_now = 0.0 if current_metrics.get("retweets") in [None, "unknown"] else float(current_metrics.get("retweets", 0))
        except Exception:
            likes_now = 0.0
            rts_now = 0.0
        spread_ratio = rts_now / max(likes_now, 1.0)
        if spread_ratio > 0.25:
            score += 0.6
        elif spread_ratio > 0.15:
            score += 0.3
        
        # Acceleration bonus
        if acceleration:
            likes_accel = acceleration.get("likes_acceleration", 0) or 0
            if likes_accel > 0:
                score += 0.5  # Bonus for positive acceleration
        
        # Classify
        if score >= 4.0:
            return True, "BREAKOUT_TRAJECTORY", score
        elif score >= 2.0:
            return True, "STRONG_GROWTH", score
        elif score >= self.composite_cutoff:
            return True, "VIRAL_CANDIDATE", score
        else:
            return False, "NORMAL", score
    
    def analyze_tweet(
        self,
        tweet_id: str,
        account: str,
        tweet_data: Dict
    ) -> Optional[Dict]:
        """
        Analyze a tweet for viral potential
        
        Returns:
            Dict with analysis results or None if not viral
        """
        # Load snapshots
        snapshots = self.load_snapshots(tweet_id)
        
        if len(snapshots) < 2:
            return None  # Need at least 2 snapshots
        
        # Get current metrics (latest snapshot)
        current_metrics = snapshots[-1]
        
        # Calculate velocity
        velocity = self.calculate_velocity(snapshots)
        if not velocity:
            return None
        
        # Calculate acceleration
        acceleration = self.calculate_acceleration(snapshots)
        
        # Classify
        is_viral, classification, score = self.classify_viral(
            tweet_id, account, current_metrics, velocity, acceleration, snapshots
        )
        
        if not is_viral:
            return None
        
        # Return analysis
        return {
            "tweet_id": tweet_id,
            "account": account,
            "tweet": tweet_data,
            "metrics": current_metrics,
            "velocity": velocity,
            "acceleration": acceleration,
            "classification": classification,
            "score": score,
            "confirmed": score >= 2.0  # Confirmed if score >= 2.0
        }
