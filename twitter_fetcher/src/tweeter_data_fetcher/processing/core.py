#!/usr/bin/env python3
from __future__ import annotations
"""
Tweet set extraction and mathematical set operations.
"""


from datetime import datetime
from typing import Any, Dict, List, Optional

from tweeter_data_fetcher.storage.facade import _format_jalali, extract_translation_meta

try:
    import jdatetime
except ImportError:
    jdatetime = None

try:
    import pytz
except ImportError:
    pytz = None


class TweetSetProcessor:
    """Parse raw pages and compute A/B/union/intersection/difference sets."""

    def extract_tweets_from_raw(
        self,
        raw_pages: List[Dict[str, Any]],
        username: Optional[str] = None,
        source_endpoint: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Extract tweets from GraphQL timeline instructions.
        Returns dict[tweet_key] = tweet_object for inherent deduplication.
        """
        result: Dict[str, Dict[str, Any]] = {}
        if not isinstance(raw_pages, list):
            return result

        for page in raw_pages:
            instructions = (
                page.get("data", {})
                .get("user", {})
                .get("result", {})
                .get("timeline", {})
                .get("timeline", {})
                .get("instructions", [])
            )
            if not isinstance(instructions, list):
                continue

            for inst in instructions:
                if not isinstance(inst, dict):
                    continue
                if inst.get("type") != "TimelineAddEntries":
                    continue
                entries = inst.get("entries", [])
                if not isinstance(entries, list):
                    continue

                for entry in entries:
                    if not isinstance(entry, dict):
                        continue

                    tweet_candidate = self._extract_tweet_from_entry(entry)
                    if not tweet_candidate:
                        continue

                    tweet_candidate = self._normalize_tweet(tweet_candidate, username=username, source_endpoint=source_endpoint)
                    key = self._tweet_key(tweet_candidate)
                    if key:
                        result[key] = tweet_candidate

                    content = entry.get("content", {})
                    if isinstance(content, dict):
                        for module_item in content.get("items", []) if isinstance(content.get("items"), list) else []:
                            if not isinstance(module_item, dict):
                                continue
                            item = module_item.get("item", {})
                            if not isinstance(item, dict):
                                continue
                            module_tweet = self._extract_tweet_from_item(item)
                            if not module_tweet:
                                continue
                            module_tweet = self._normalize_tweet(module_tweet, username=username, source_endpoint=source_endpoint)
                            module_key = self._tweet_key(module_tweet)
                            if module_key:
                                result[module_key] = module_tweet

        return result

    def _extract_tweet_from_entry(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        content = entry.get("content", {})
        if not isinstance(content, dict):
            return None
        item_content = content.get("itemContent", {})
        if not isinstance(item_content, dict):
            return None
        return self._extract_tweet_from_item(item_content)

    def _extract_tweet_from_item(self, item_content: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        tweet_results = item_content.get("tweet_results", {})
        if not isinstance(tweet_results, dict):
            return None
        tweet_obj = tweet_results.get("result")
        if not isinstance(tweet_obj, dict):
            return None
        tweet_obj = self._unwrap_tweet_result(tweet_obj)

        legacy = tweet_obj.get("legacy", {})
        if not isinstance(legacy, dict):
            return None
        if not tweet_obj.get("rest_id"):
            return None
        return tweet_obj

    @staticmethod
    def _unwrap_tweet_result(tweet_obj: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(tweet_obj.get("tweet"), dict):
            return tweet_obj["tweet"]
        return tweet_obj

    @staticmethod
    def _tweet_key(tweet_obj: Dict[str, Any]) -> Optional[str]:
        tweet_id = tweet_obj.get("rest_id") or tweet_obj.get("id")
        if not tweet_id:
            return None
        author_id = tweet_obj.get("author_id")
        if not author_id:
            author_id = (
                tweet_obj.get("core", {})
                .get("user_results", {})
                .get("result", {})
                .get("rest_id")
            )
        if author_id:
            return f"{author_id}:{tweet_id}"
        return str(tweet_id)

    def _normalize_tweet(
        self,
        tweet_obj: Dict[str, Any],
        username: Optional[str] = None,
        source_endpoint: Optional[str] = None,
    ) -> Dict[str, Any]:
        legacy = tweet_obj.get("legacy", {}) if isinstance(tweet_obj.get("legacy"), dict) else {}
        core_user = (
            tweet_obj.get("core", {})
            .get("user_results", {})
            .get("result", {})
        )
        author = self._user_summary(core_user)
        author_handle = author.get("handle") or username or "unknown"
        tweet_id = str(tweet_obj.get("rest_id") or "")
        entities = self._extract_entities(legacy)

        normalized: Dict[str, Any] = {
            "id": tweet_id,
            "rest_id": tweet_id,
            "author_id": author.get("id"),
            "account": str(author_handle).lstrip("@"),
            "author": author,
            "author_display_name": author.get("display_name"),
            "author_avatar_url": author.get("avatar_url"),
            "author_verified": author.get("verified", False),
            "author_verified_type": author.get("verified_type"),
            "timestamp": self._format_timestamp(legacy.get("created_at")),
            "created_at": legacy.get("created_at"),
            "raw_timestamp": legacy.get("created_at"),
            "text": self._tweet_text(tweet_obj),
            "url": f"https://x.com/{str(author_handle).lstrip('@')}/status/{tweet_id}" if tweet_id else "",
            "likes": legacy.get("favorite_count", 0),
            "retweets": legacy.get("retweet_count", 0),
            "replies": legacy.get("reply_count", 0),
            "quotes": legacy.get("quote_count", 0),
            "bookmarks": legacy.get("bookmark_count", 0),
            "views": self._view_count(tweet_obj),
            "entities": entities,
            "media": entities.get("media", []),
            "card": self._extract_card(tweet_obj),
            "possibly_sensitive": bool(legacy.get("possibly_sensitive")),
            "source_language": legacy.get("lang"),
            "translation_meta": extract_translation_meta(tweet_obj),
            "conversation_id": legacy.get("conversation_id_str"),
            "in_reply_to_status_id": legacy.get("in_reply_to_status_id_str"),
            "in_reply_to_user_id": legacy.get("in_reply_to_user_id_str"),
            "in_reply_to_screen_name": legacy.get("in_reply_to_screen_name"),
            "reply_to": {
                "tweet_id": legacy.get("in_reply_to_status_id_str"),
                "user_id": legacy.get("in_reply_to_user_id_str"),
                "handle": legacy.get("in_reply_to_screen_name"),
            } if legacy.get("in_reply_to_status_id_str") else None,
            "type": "Tweet",
        }
        if source_endpoint:
            normalized["source_endpoint"] = source_endpoint

        retweeted = self._nested_tweet(tweet_obj, "retweeted_status_result") or self._nested_tweet(legacy, "retweeted_status_result")
        quoted = self._nested_tweet(tweet_obj, "quoted_status_result")

        if retweeted:
            retweet_legacy = retweeted.get("legacy", {}) if isinstance(retweeted.get("legacy"), dict) else {}
            retweet_user = (
                retweeted.get("core", {})
                .get("user_results", {})
                .get("result", {})
            )
            retweet_author = self._user_summary(retweet_user)
            normalized.update({
                "type": "Retweet",
                "retweeted_tweet_id": str(retweeted.get("rest_id") or ""),
                "retweeted_author": retweet_author.get("handle"),
                "retweeted_text": self._tweet_text(retweeted),
                "retweeted_timestamp": self._format_timestamp(retweet_legacy.get("created_at")),
                "retweeted_translation_meta": extract_translation_meta(retweeted),
                "retweeted_tweet": self._tweet_summary(retweeted),
            })
        elif str(normalized.get("text") or "").startswith("RT @"):
            normalized.update({
                "type": "Retweet",
                "retweeted_tweet_id": None,
                "retweeted_author": None,
                "retweeted_text": normalized.get("text", ""),
                "retweeted_timestamp": "",
                "retweeted_translation_meta": None,
            })
        elif quoted:
            quoted_legacy = quoted.get("legacy", {}) if isinstance(quoted.get("legacy"), dict) else {}
            quoted_user = (
                quoted.get("core", {})
                .get("user_results", {})
                .get("result", {})
            )
            quoted_author = self._user_summary(quoted_user)
            normalized.update({
                "type": "Quote",
                "quoted_tweet_id": str(quoted.get("rest_id") or ""),
                "quoted_author": quoted_author.get("handle"),
                "quoted_text": self._tweet_text(quoted),
                "quoted_timestamp": self._format_timestamp(quoted_legacy.get("created_at")),
                "quoted_translation_meta": extract_translation_meta(quoted),
                "quoted_tweet": self._tweet_summary(quoted),
            })
        elif legacy.get("quoted_status_id_str"):
            normalized.update({
                "type": "Quote",
                "quoted_tweet_id": legacy.get("quoted_status_id_str"),
                "quoted_author": None,
                "quoted_text": "",
                "quoted_timestamp": "",
                "quoted_translation_meta": None,
            })
        elif normalized.get("in_reply_to_status_id"):
            normalized["type"] = "Reply"

        return normalized

    @staticmethod
    def _user_summary(user: Any) -> Dict[str, Any]:
        user = user if isinstance(user, dict) else {}
        legacy = user.get("legacy", {}) if isinstance(user.get("legacy"), dict) else {}
        core = user.get("core", {}) if isinstance(user.get("core"), dict) else {}
        avatar = user.get("avatar", {}) if isinstance(user.get("avatar"), dict) else {}
        verification = user.get("verification", {}) if isinstance(user.get("verification"), dict) else {}
        handle = core.get("screen_name") or legacy.get("screen_name")
        return {
            "id": str(user.get("rest_id")) if user.get("rest_id") is not None else None,
            "handle": str(handle).lstrip("@") if handle else None,
            "display_name": core.get("name") or legacy.get("name"),
            "avatar_url": avatar.get("image_url") or legacy.get("profile_image_url_https") or legacy.get("profile_image_url"),
            "verified": bool(user.get("is_blue_verified") or verification.get("verified") or legacy.get("verified")),
            "verified_type": verification.get("verified_type") or verification.get("type"),
        }

    @staticmethod
    def _tweet_text(tweet_obj: Dict[str, Any]) -> str:
        note_result = (
            tweet_obj.get("note_tweet", {})
            .get("note_tweet_results", {})
            .get("result", {})
        )
        legacy = tweet_obj.get("legacy", {}) if isinstance(tweet_obj.get("legacy"), dict) else {}
        return str(
            (note_result.get("text") if isinstance(note_result, dict) else None)
            or legacy.get("full_text")
            or legacy.get("text")
            or ""
        )

    @classmethod
    def _tweet_summary(cls, tweet_obj: Dict[str, Any]) -> Dict[str, Any]:
        legacy = tweet_obj.get("legacy", {}) if isinstance(tweet_obj.get("legacy"), dict) else {}
        user = (
            tweet_obj.get("core", {})
            .get("user_results", {})
            .get("result", {})
        )
        entities = cls._extract_entities(legacy)
        return {
            "id": str(tweet_obj.get("rest_id") or ""),
            "author": cls._user_summary(user),
            "text": cls._tweet_text(tweet_obj),
            "created_at": legacy.get("created_at"),
            "media": entities.get("media", []),
            "metrics": {
                "likes": legacy.get("favorite_count", 0),
                "retweets": legacy.get("retweet_count", 0),
                "replies": legacy.get("reply_count", 0),
                "quotes": legacy.get("quote_count", 0),
                "bookmarks": legacy.get("bookmark_count", 0),
                "views": cls._view_count(tweet_obj),
            },
        }

    @staticmethod
    def _view_count(tweet_obj: Dict[str, Any]) -> int:
        views = tweet_obj.get("views", {}) if isinstance(tweet_obj, dict) else {}
        raw = views.get("count", 0) if isinstance(views, dict) else 0
        try:
            return int(str(raw).replace(",", ""))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _extract_entities(legacy: Dict[str, Any]) -> Dict[str, Any]:
        entities = legacy.get("entities", {}) if isinstance(legacy.get("entities"), dict) else {}
        extended = legacy.get("extended_entities", {}) if isinstance(legacy.get("extended_entities"), dict) else {}
        media = extended.get("media", entities.get("media", []))
        normalized_media = []
        for item in media if isinstance(media, list) else []:
            if not isinstance(item, dict):
                continue
            video_info = item.get("video_info", {}) if isinstance(item.get("video_info"), dict) else {}
            original = item.get("original_info", {}) if isinstance(item.get("original_info"), dict) else {}
            normalized_media.append({
                "id": str(item.get("id_str") or item.get("id") or ""),
                "type": item.get("type"),
                "url": item.get("media_url_https") or item.get("media_url"),
                "expanded_url": item.get("expanded_url"),
                "alt_text": item.get("ext_alt_text"),
                "width": original.get("width"),
                "height": original.get("height"),
                "duration_ms": video_info.get("duration_millis"),
                "aspect_ratio": video_info.get("aspect_ratio"),
                "variants": [
                    {
                        "url": variant.get("url"),
                        "content_type": variant.get("content_type"),
                        "bitrate": variant.get("bitrate"),
                    }
                    for variant in video_info.get("variants", [])
                    if isinstance(variant, dict) and variant.get("url")
                ],
            })
        return {
            "urls": [
                {
                    "short": item.get("url"),
                    "expanded": item.get("expanded_url") or item.get("display_url"),
                }
                for item in entities.get("urls", [])
                if isinstance(item, dict)
            ],
            "hashtags": [item.get("text") for item in entities.get("hashtags", []) if isinstance(item, dict) and item.get("text")],
            "mentions": [
                {"handle": item.get("screen_name"), "name": item.get("name")}
                for item in entities.get("user_mentions", [])
                if isinstance(item, dict)
            ],
            "media_links": [item.get("media_url_https") or item.get("expanded_url") for item in media if isinstance(item, dict) and (item.get("media_url_https") or item.get("expanded_url"))],
            "media_types": [item.get("type") for item in media if isinstance(item, dict) and item.get("type")],
            "media": normalized_media,
        }

    @staticmethod
    def _extract_card(tweet_obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        card = tweet_obj.get("card", {}) if isinstance(tweet_obj.get("card"), dict) else {}
        legacy = card.get("legacy", {}) if isinstance(card.get("legacy"), dict) else {}
        bindings = legacy.get("binding_values", []) if isinstance(legacy.get("binding_values"), list) else []
        values: Dict[str, Any] = {}
        for binding in bindings:
            if not isinstance(binding, dict) or not binding.get("key"):
                continue
            value = binding.get("value", {}) if isinstance(binding.get("value"), dict) else {}
            image = value.get("image_value", {}) if isinstance(value.get("image_value"), dict) else {}
            values[str(binding["key"])] = value.get("string_value") or image.get("url")
        if not values:
            return None
        return {
            "title": values.get("title") or values.get("summary_title"),
            "description": values.get("description") or values.get("summary_description"),
            "url": values.get("card_url") or values.get("vanity_url"),
            "image_url": values.get("photo_image_full_size_original") or values.get("summary_photo_image_original"),
        }

    @classmethod
    def _nested_tweet(cls, tweet_obj: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
        nested = tweet_obj.get(key, {})
        if not isinstance(nested, dict):
            return None
        result = nested.get("result")
        if isinstance(result, dict):
            return cls._unwrap_tweet_result(result)
        return None

    @staticmethod
    def _format_timestamp(created_at: Any) -> str:
        raw = str(created_at or "").strip()
        if not raw:
            return "UNKNOWN"
        try:
            dt = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
            if pytz:
                dt = dt.astimezone(pytz.timezone("Asia/Tehran"))
            return _format_jalali(dt, "%Y-%m-%d %H:%M:%S") + " Asia/Tehran"
        except Exception:
            return raw

    def get_union(self, set_a: Dict[str, Dict[str, Any]], set_b: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged = dict(set_a or {})
        merged.update(set_b or {})
        return list(merged.values())

    def get_intersection(self, set_a: Dict[str, Dict[str, Any]], set_b: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        keys = set((set_a or {}).keys()) & set((set_b or {}).keys())
        return [set_b[k] if k in set_b else set_a[k] for k in keys]

    def get_difference_b_minus_a(self, set_a: Dict[str, Dict[str, Any]], set_b: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        keys = set((set_b or {}).keys()) - set((set_a or {}).keys())
        return [set_b[k] for k in keys if k in set_b]

    def get_difference_a_minus_b(self, set_a: Dict[str, Dict[str, Any]], set_b: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        keys = set((set_a or {}).keys()) - set((set_b or {}).keys())
        return [set_a[k] for k in keys if k in set_a]

    def get_symmetric_difference(self, set_a: Dict[str, Dict[str, Any]], set_b: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        return self.get_difference_a_minus_b(set_a, set_b) + self.get_difference_b_minus_a(set_a, set_b)


#!/usr/bin/env python3
"""
Rolling-window coverage helpers for v4 subsystems.
"""


from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set

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


def tweet_datetime(tweet: Dict[str, Any]) -> Optional[datetime]:
    """Absolute (Tehran-aware) datetime of a tweet, for timestamp-granular windows."""
    for key in ("raw_timestamp", "created_at"):
        parsed = parse_twitter_timestamp(tweet.get(key) if isinstance(tweet, dict) else None)
        if parsed:
            return parsed
    return None


def tweet_is_older_than_jalali(tweet: Dict[str, Any], cutoff_jalali: str) -> bool:
    date_value = tweet_jalali_date(tweet)
    return bool(date_value and date_value < cutoff_jalali)


def _floor_datetime(dt: datetime, granularity: str) -> datetime:
    """Floor to start of day or start of hour (preserves tzinfo)."""
    if granularity == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def window_cutoff(
    *,
    window_hours: Optional[float] = None,
    window_days: Optional[float] = None,
    watermark: Optional[Any] = None,
    floor: str = "day",
    now_dt: Optional[datetime] = None,
) -> datetime:
    """Absolute (Tehran-aware) window start for the unified rolling window.

    cutoff = min(now - configured_window, floor(watermark)).

    The floored-watermark term guarantees the backfill reaches *past* the boundary
    of the last successful fetch — closing the partial-day/partial-hour gap where
    tweets between a mid-period fetch and the period end were silently skipped.
    When no watermark exists yet (first run) the plain rolling window is used.

    ``watermark`` may be a datetime or an ISO-8601 string (e.g. the value stored by
    StorageManager.set_fetch_watermark); unparseable strings are treated as absent.
    """
    current = now_dt or tehran_now()
    if window_hours is not None:
        base = current - timedelta(hours=max(0.0, float(window_hours)))
    elif window_days is not None:
        base = current - timedelta(days=max(0.0, float(window_days)))
    else:
        base = current
    wm = watermark
    if isinstance(wm, str):
        raw = wm.strip()
        try:
            wm = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
        except ValueError:
            wm = None
    if wm is not None:
        if pytz and wm.tzinfo is None:
            wm = pytz.utc.localize(wm).astimezone(pytz.timezone(TIMEZONE))
        elif pytz:
            wm = wm.astimezone(pytz.timezone(TIMEZONE))
        floored = _floor_datetime(wm, floor)
        return min(base, floored)
    return base


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
        crossed = bool(oldest and oldest < window_start)
        covered = [date for date in targets if date in dates]
        missing = [date for date in targets if date not in dates]

        if not item_count:
            return WindowCoverage(False, targets, covered, missing, oldest, newest, False, item_count, "no_items")
        if not crossed:
            return WindowCoverage(False, targets, covered, missing, oldest, newest, False, item_count, "window_start_not_fully_crossed")
        
        return WindowCoverage(True, targets, covered, missing, oldest, newest, True, item_count, "window_fully_crossed")

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

    def evaluate_tweets_cutoff(
        self,
        tweets: Iterable[Dict[str, Any]],
        cutoff: datetime,
    ) -> WindowCoverage:
        """Timestamp-granular completeness: proven when the oldest tweet seen is at or
        before an absolute ``cutoff`` datetime (i.e. pagination has walked back past the
        window start). Shared by both pipelines so live (hours) and historical (days)
        use one mechanism. ``cutoff`` should be tz-aware (Tehran) to match tweet times."""
        oldest_dt: Optional[datetime] = None
        newest_dt: Optional[datetime] = None
        item_count = 0
        for tweet in tweets:
            item_count += 1
            dt = tweet_datetime(tweet)
            if dt is None:
                continue
            if oldest_dt is None or dt < oldest_dt:
                oldest_dt = dt
            if newest_dt is None or dt > newest_dt:
                newest_dt = dt

        oldest_s = jalali_date(oldest_dt) if oldest_dt else None
        newest_s = jalali_date(newest_dt) if newest_dt else None
        cutoff_s = jalali_date(cutoff)
        crossed = bool(oldest_dt and oldest_dt <= cutoff)

        if not item_count:
            return WindowCoverage(False, [cutoff_s], [], [cutoff_s], oldest_s, newest_s, False, item_count, "no_items")
        if not crossed:
            return WindowCoverage(False, [cutoff_s], [], [], oldest_s, newest_s, False, item_count, "cutoff_not_crossed")
        return WindowCoverage(True, [cutoff_s], [cutoff_s], [], oldest_s, newest_s, True, item_count, "cutoff_crossed")

    def evaluate_raw_pages_cutoff(
        self,
        raw_pages: List[Dict[str, Any]],
        username: str,
        endpoint: str,
        cutoff: datetime,
    ) -> WindowCoverage:
        tweets = self.extract_endpoint_tweets(raw_pages, username=username, endpoint=endpoint)
        return self.evaluate_tweets_cutoff(tweets, cutoff=cutoff)


"""Shared X GraphQL request/response contracts and semantic validation."""


from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


TARGET_ENDPOINTS = {"UserTweets", "UserTweetsAndReplies", "SearchTimeline"}

DATA_PATHS: Dict[str, Tuple[str, ...]] = {
    "UserByScreenName": ("data", "user", "result"),
    "UserTweets": ("data", "user", "result", "timeline", "timeline"),
    "UserTweetsAndReplies": ("data", "user", "result", "timeline", "timeline"),
    "SearchTimeline": ("data", "search_by_raw_query", "search_timeline", "timeline"),
}

USER_BY_SCREEN_NAME_FEATURES: Dict[str, Any] = {
    "hidden_profile_subscriptions_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}

USER_BY_SCREEN_NAME_FIELD_TOGGLES: Dict[str, Any] = {
    "withPayments": False,
    "withAuxiliaryUserLabels": True,
}

SEARCH_TIMELINE_FEATURES: Dict[str, Any] = {
    "articles_preview_enabled": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_inline_media_enabled": False,
    "longform_notetweets_rich_text_read_enabled": True,
    "post_ctas_fetch_enabled": False,
    "premium_content_api_read_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_grok_analysis_button_from_backend": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_profile_redirect_enabled": False,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "rweb_cashtags_composer_attachment_enabled": True,
    "rweb_cashtags_enabled": True,
    "rweb_conversational_replies_downvote_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "rweb_video_screen_enabled": False,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "verified_phone_label_enabled": False,
    "view_counts_everywhere_api_enabled": True,
}


@dataclass(frozen=True)
class GraphQLValidation:
    ok: bool
    endpoint: str
    reason: str
    errors: List[Any]
    data: Optional[Dict[str, Any]]
    instructions: List[Dict[str, Any]]


def user_by_screen_name_contract(username: str) -> Dict[str, Any]:
    return {
        "variables": {"screen_name": username, "withGrokTranslatedBio": True},
        "features": dict(USER_BY_SCREEN_NAME_FEATURES),
        "fieldToggles": dict(USER_BY_SCREEN_NAME_FIELD_TOGGLES),
    }


def timeline_variables(endpoint: str, user_id: str, cursor: Optional[str] = None) -> Dict[str, Any]:
    variables: Dict[str, Any] = {
        "userId": user_id,
        "count": 20,
        "includePromotedContent": True,
    }
    if endpoint == "UserTweetsAndReplies":
        variables["withCommunity"] = True
        variables["withVoice"] = True
    elif endpoint == "UserTweets":
        variables["withQuickPromoteEligibilityTweetFields"] = True
        variables["withVoice"] = True
    else:
        raise ValueError(f"Unsupported timeline endpoint: {endpoint}")
    if cursor:
        variables["cursor"] = cursor
    return variables


def timeline_field_toggles(endpoint: str) -> Optional[Dict[str, Any]]:
    if endpoint == "SearchTimeline":
        return None
    if endpoint in {"UserTweets", "UserTweetsAndReplies"}:
        return {"withArticlePlainText": False}
    return None


def search_timeline_variables(
    *,
    raw_query: str,
    product: str,
    count: int = 20,
    query_source: str = "typed_query",
    cursor: Optional[str] = None,
    with_grok_translated_bio: bool = True,
    with_quick_promote_eligibility_tweet_fields: bool = False,
) -> Dict[str, Any]:
    variables: Dict[str, Any] = {
        "rawQuery": raw_query,
        "count": max(1, min(int(count), 20)),
        "querySource": query_source,
        "product": product,
        "withGrokTranslatedBio": with_grok_translated_bio,
        "withQuickPromoteEligibilityTweetFields": with_quick_promote_eligibility_tweet_fields,
    }
    if cursor:
        variables["cursor"] = cursor
    return variables


def value_at_path(payload: Any, path: Tuple[str, ...]) -> Any:
    value = payload
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def validate_graphql_payload(endpoint: str, payload: Any) -> GraphQLValidation:
    """Reject HTTP-200 GraphQL failures before they reach storage/parsers."""
    if not isinstance(payload, dict):
        return GraphQLValidation(False, endpoint, "payload_not_object", [], None, [])
    errors = payload.get("errors")
    if errors:
        values = errors if isinstance(errors, list) else [errors]
        return GraphQLValidation(False, endpoint, "graphql_errors", values, None, [])
    path = DATA_PATHS.get(endpoint)
    if not path:
        return GraphQLValidation(False, endpoint, "unknown_endpoint_contract", [], None, [])
    data = value_at_path(payload, path)
    if not isinstance(data, dict):
        return GraphQLValidation(False, endpoint, "expected_data_missing", [], None, [])
    if endpoint == "UserByScreenName":
        if not data.get("rest_id"):
            return GraphQLValidation(False, endpoint, "user_rest_id_missing", [], data, [])
        return GraphQLValidation(True, endpoint, "ok", [], data, [])
    instructions = data.get("instructions")
    if not isinstance(instructions, list):
        return GraphQLValidation(False, endpoint, "instructions_missing", [], data, [])
    supported = {
        "TimelineAddEntries", "TimelineReplaceEntry", "TimelinePinEntry",
        "TimelineClearCache", "TimelineGeneralContext",
    }
    typed = [item for item in instructions if isinstance(item, dict)]
    if typed and not any(str(item.get("type")) in supported for item in typed):
        return GraphQLValidation(False, endpoint, "unsupported_instruction_set", [], data, typed)
    return GraphQLValidation(True, endpoint, "ok", [], data, typed)


def extract_bottom_cursor(payload: Any) -> Optional[str]:
    """Find the opaque Bottom cursor in direct or nested timeline entries."""
    stack = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            cursor = value.get("value")
            cursor_type = str(value.get("cursorType", "")).lower()
            entry_id = str(value.get("entryId", "")).lower()
            if cursor and (cursor_type == "bottom" or entry_id.startswith("cursor-bottom-")):
                return str(cursor)
            stack.extend(reversed(list(value.values())))
        elif isinstance(value, list):
            stack.extend(reversed(value))
    return None
