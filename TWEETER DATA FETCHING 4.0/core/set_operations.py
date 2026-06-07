#!/usr/bin/env python3
"""
Tweet set extraction and mathematical set operations.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from exporters.text_export_helper import extract_translation_meta

try:
    import jdatetime
except ImportError:
    jdatetime = None

try:
    import pytz
except ImportError:
    pytz = None


def _gregorian_to_jalali(year: int, month: int, day: int) -> tuple[int, int, int]:
    g_days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    j_days_in_month = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]
    gy = year - 1600
    gm = month - 1
    gd = day - 1
    g_day_no = 365 * gy + (gy + 3) // 4 - (gy + 99) // 100 + (gy + 399) // 400
    for idx in range(gm):
        g_day_no += g_days_in_month[idx]
    if gm > 1 and ((gy + 1600) % 4 == 0 and ((gy + 1600) % 100 != 0 or (gy + 1600) % 400 == 0)):
        g_day_no += 1
    g_day_no += gd
    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    jm = 0
    while jm < 11 and j_day_no >= j_days_in_month[jm]:
        j_day_no -= j_days_in_month[jm]
        jm += 1
    return jy, jm + 1, j_day_no + 1


def _format_jalali(dt: datetime) -> str:
    if jdatetime:
        jalali = jdatetime.datetime.fromgregorian(datetime=dt)
        return jalali.strftime("%Y-%m-%d %H:%M:%S") + " Asia/Tehran"
    jy, jm, jd = _gregorian_to_jalali(dt.year, dt.month, dt.day)
    return f"{jy:04d}-{jm:02d}-{jd:02d} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} Asia/Tehran"


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
        user_legacy = core_user.get("legacy", {}) if isinstance(core_user, dict) and isinstance(core_user.get("legacy"), dict) else {}
        author_handle = user_legacy.get("screen_name") or username or "unknown"
        tweet_id = str(tweet_obj.get("rest_id") or "")

        normalized: Dict[str, Any] = {
            "id": tweet_id,
            "rest_id": tweet_id,
            "author_id": core_user.get("rest_id") if isinstance(core_user, dict) else None,
            "account": str(author_handle).lstrip("@"),
            "timestamp": self._format_timestamp(legacy.get("created_at")),
            "created_at": legacy.get("created_at"),
            "raw_timestamp": legacy.get("created_at"),
            "text": legacy.get("full_text") or legacy.get("text") or "",
            "url": f"https://x.com/{str(author_handle).lstrip('@')}/status/{tweet_id}" if tweet_id else "",
            "likes": legacy.get("favorite_count", 0),
            "retweets": legacy.get("retweet_count", 0),
            "replies": legacy.get("reply_count", 0),
            "quotes": legacy.get("quote_count", 0),
            "bookmarks": legacy.get("bookmark_count", 0),
            "views": self._view_count(tweet_obj),
            "entities": self._extract_entities(legacy),
            "source_language": legacy.get("lang"),
            "translation_meta": extract_translation_meta(tweet_obj),
            "conversation_id": legacy.get("conversation_id_str"),
            "in_reply_to_status_id": legacy.get("in_reply_to_status_id_str"),
            "in_reply_to_user_id": legacy.get("in_reply_to_user_id_str"),
            "in_reply_to_screen_name": legacy.get("in_reply_to_screen_name"),
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
            retweet_user_legacy = retweet_user.get("legacy", {}) if isinstance(retweet_user, dict) and isinstance(retweet_user.get("legacy"), dict) else {}
            normalized.update({
                "type": "Retweet",
                "retweeted_tweet_id": str(retweeted.get("rest_id") or ""),
                "retweeted_author": retweet_user_legacy.get("screen_name"),
                "retweeted_text": retweet_legacy.get("full_text") or retweet_legacy.get("text") or "",
                "retweeted_timestamp": self._format_timestamp(retweet_legacy.get("created_at")),
                "retweeted_translation_meta": extract_translation_meta(retweeted),
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
            quoted_user_legacy = quoted_user.get("legacy", {}) if isinstance(quoted_user, dict) and isinstance(quoted_user.get("legacy"), dict) else {}
            normalized.update({
                "type": "Quote",
                "quoted_tweet_id": str(quoted.get("rest_id") or ""),
                "quoted_author": quoted_user_legacy.get("screen_name"),
                "quoted_text": quoted_legacy.get("full_text") or quoted_legacy.get("text") or "",
                "quoted_timestamp": self._format_timestamp(quoted_legacy.get("created_at")),
                "quoted_translation_meta": extract_translation_meta(quoted),
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
            return _format_jalali(dt)
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
