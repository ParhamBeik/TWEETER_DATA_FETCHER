#!/usr/bin/env python3
"""
Rolling-window coverage helpers for v4 subsystems.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set

from shared.core.set_operations import TweetSetProcessor
from shared.data_pipeline.storage_manager import _format_jalali

try:
    import pytz
except ImportError:  # pragma: no cover
    pytz = None


TIMEZONE = "Asia/Tehran"


@dataclass(frozen=True)
class WindowCoverage:
    complete: bool
    target_dates: List[str]
    covered_dates: List[str]
    missing_dates: List[str]
    oldest_date: Optional[str]
    newest_date: Optional[str]
    crossed_window_start: bool
    item_count: int
    reason: str


def tehran_now() -> datetime:
    if pytz:
        return datetime.now(pytz.timezone(TIMEZONE))
    return datetime.utcnow()


def jalali_date(dt: datetime) -> str:
    if pytz and dt.tzinfo is None:
        dt = pytz.utc.localize(dt).astimezone(pytz.timezone(TIMEZONE))
    elif pytz:
        dt = dt.astimezone(pytz.timezone(TIMEZONE))
    return _format_jalali(dt, "%Y-%m-%d")


def target_jalali_dates(days: int, now_dt: Optional[datetime] = None) -> List[str]:
    current = now_dt or tehran_now()
    return [jalali_date(current - timedelta(days=offset)) for offset in range(max(1, int(days)))]


def parse_twitter_timestamp(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
        if pytz:
            return parsed.astimezone(pytz.timezone(TIMEZONE))
        return parsed
    except Exception:
        return None


def tweet_jalali_date(tweet: Dict[str, Any]) -> Optional[str]:
    for key in ("raw_timestamp", "created_at"):
        parsed = parse_twitter_timestamp(tweet.get(key) if isinstance(tweet, dict) else None)
        if parsed:
            return jalali_date(parsed)
    timestamp = str(tweet.get("timestamp") or "" if isinstance(tweet, dict) else "")
    if len(timestamp) >= 10 and timestamp[:4].isdigit():
        return timestamp[:10]
    return None


def tweet_is_older_than_jalali(tweet: Dict[str, Any], cutoff_jalali: str) -> bool:
    date_value = tweet_jalali_date(tweet)
    return bool(date_value and date_value < cutoff_jalali)


class RollingWindowEvaluator:
    """Evaluate whether raw pages prove a Tehran/Jalali rolling window."""

    def __init__(self):
        self.processor = TweetSetProcessor()

    def extract_endpoint_tweets(
        self,
        raw_pages: List[Dict[str, Any]],
        username: str,
        endpoint: str,
    ) -> List[Dict[str, Any]]:
        extracted = self.processor.extract_tweets_from_raw(raw_pages, username=username, source_endpoint=endpoint)
        return list(extracted.values())

    def evaluate_tweets(
        self,
        tweets: Iterable[Dict[str, Any]],
        window_days: int,
        now_dt: Optional[datetime] = None,
    ) -> WindowCoverage:
        targets = target_jalali_dates(window_days, now_dt=now_dt)
        if not targets:
            targets = target_jalali_dates(1, now_dt=now_dt)
        window_start = targets[-1]
        dates: Set[str] = set()
        item_count = 0
        for tweet in tweets:
            item_count += 1
            date_value = tweet_jalali_date(tweet)
            if date_value:
                dates.add(date_value)

        sorted_dates = sorted(dates)
        oldest = sorted_dates[0] if sorted_dates else None
        newest = sorted_dates[-1] if sorted_dates else None
        crossed = bool(oldest and oldest <= window_start)
        covered = [date for date in targets if date in dates]
        missing = [date for date in targets if date not in dates]

        if not item_count:
            return WindowCoverage(False, targets, covered, missing, oldest, newest, False, item_count, "no_items")
        if not crossed:
            return WindowCoverage(False, targets, covered, missing, oldest, newest, False, item_count, "window_start_not_crossed")
        
        return WindowCoverage(True, targets, covered, missing, oldest, newest, True, item_count, "window_crossed")

    def evaluate_raw_pages(
        self,
        raw_pages: List[Dict[str, Any]],
        username: str,
        endpoint: str,
        window_days: int,
        now_dt: Optional[datetime] = None,
    ) -> WindowCoverage:
        tweets = self.extract_endpoint_tweets(raw_pages, username=username, endpoint=endpoint)
        return self.evaluate_tweets(tweets, window_days=window_days, now_dt=now_dt)
