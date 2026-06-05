#!/usr/bin/env python3
"""
Storage Manager - Centralized storage and deduplication

Responsibilities:
- Folder structure creation
- Global dedupe registry (seen_tweets.json)
- Snapshot storage (append-only time-series)
- Output formatting (beautiful viral reports)
- File writing with atomic operations
- Endpoint comparison logic
"""

import json
import re
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import jdatetime
    import pytz
except ImportError:
    print("ERROR: Missing dependencies. Run: pip3 install jdatetime pytz")
    raise

from text_export_helper import choose_export_text


def extract_metrics(tweet_obj: Dict) -> Dict:
    """Extract engagement metrics from a raw tweet GraphQL object."""
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
    """Centralized storage and deduplication manager"""

    def __init__(self, base_dir: Optional[Path] = None, timezone: str = "Asia/Tehran"):
        self.base_dir = base_dir or Path(__file__).parent
        self.tz = pytz.timezone(timezone)
        self.config = self._load_config()

        # Create folder structure
        self.data_dir = self.base_dir / "data"
        self.user_tweets_dir = self.data_dir / "USER_TWEETS"
        self.user_replies_dir = self.data_dir / "USER_TWEETS_AND_REPLIES"
        self.endpoint_diffs_dir = self.data_dir / "ENDPOINT_DIFFS"
        self.search_timeline_dir = self.data_dir / "SEARCH_TIMELINE"
        self.merged_dir = self.data_dir / "MERGED_TIMELINES"
        self.snapshots_dir = self.data_dir / "SNAPSHOTS"
        self.viral_dir = self.data_dir / "VIRAL"
        self.viral_candidates_dir = self.viral_dir / "candidates"
        self.viral_confirmed_dir = self.viral_dir / "confirmed"
        self.legacy_viral_candidates_dir = self.viral_dir / "CANDIDATES"
        self.legacy_viral_confirmed_dir = self.viral_dir / "CONFIRMED"
        self.state_dir = self.data_dir / "STATE"
        self.logs_dir = self.base_dir / "logs"
        self.snapshot_index_file = self.state_dir / "snapshot_index.json"

        for directory in [
            self.user_tweets_dir,
            self.user_replies_dir,
            self.endpoint_diffs_dir,
            self.search_timeline_dir,
            self.merged_dir,
            self.snapshots_dir,
            self.viral_candidates_dir,
            self.viral_confirmed_dir,
            self.legacy_viral_candidates_dir,
            self.legacy_viral_confirmed_dir,
            self.state_dir,
            self.logs_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)

        self.seen_tweets: Dict[str, Dict] = self._load_seen_tweets()
        self.snapshot_index: Dict[str, str] = self._load_snapshot_index()

    def _load_config(self) -> Dict:
        """Load project config for snapshot thresholds and runtime options."""
        config_file = self.base_dir / "config.json"
        if config_file.exists():
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _load_seen_tweets(self) -> Dict[str, Dict]:
        """Load global tweet registry"""
        registry_file = self.state_dir / "seen_tweets.json"
        if registry_file.exists():
            try:
                with open(registry_file) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_seen_tweets(self):
        """Persist tweet registry"""
        registry_file = self.state_dir / "seen_tweets.json"
        with open(registry_file, "w") as f:
            json.dump(self.seen_tweets, f, indent=2)

    def _load_snapshot_index(self) -> Dict[str, str]:
        """Load tweet_id -> snapshot relative path map."""
        if self.snapshot_index_file.exists():
            try:
                with open(self.snapshot_index_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
            except Exception:
                pass
        return {}

    def _save_snapshot_index(self):
        """Persist snapshot index state."""
        with open(self.snapshot_index_file, "w", encoding="utf-8") as f:
            json.dump(self.snapshot_index, f, indent=2)

    def _safe_slug(self, text: str, max_len: int = 42) -> str:
        """Convert tweet text to a filename-safe slug."""
        if not text:
            return "no_text"
        compact = re.sub(r"https?://\S+", "", str(text)).strip()
        compact = compact.encode("ascii", "ignore").decode("ascii")
        compact = re.sub(r"[^A-Za-z0-9]+", "_", compact)
        compact = re.sub(r"_+", "_", compact).strip("_")
        if not compact:
            compact = "no_text"
        return compact[:max_len].rstrip("_") or "no_text"

    def _safe_username(self, username: str) -> str:
        base = (username or "unknown").strip().lower()
        base = re.sub(r"[^a-z0-9_]+", "_", base)
        base = re.sub(r"_+", "_", base).strip("_")
        return base or "unknown"

    def _safe_type(self, tweet_type: str) -> str:
        value = (tweet_type or "tweet").strip().upper()
        value = re.sub(r"[^A-Z0-9]+", "_", value)
        value = re.sub(r"_+", "_", value).strip("_")
        return value or "TWEET"

    def _tweet_kind(self, tweet_type: str) -> str:
        """Normalize tweet type for filename readability."""
        value = (tweet_type or "tweet").strip().lower()
        mapping = {
            "tweet": "tweet",
            "reply": "reply",
            "retweet": "retweet",
            "quote": "quote",
        }
        return mapping.get(value, "tweet")

    def _jalali_tehran_parts(self, now: Optional[datetime] = None) -> Tuple[str, str]:
        """Return Jalali date/time in Tehran timezone-safe format."""
        current = now or datetime.now(self.tz)
        jalali_now = jdatetime.datetime.fromgregorian(datetime=current)
        return jalali_now.strftime("%Y-%m-%d"), jalali_now.strftime("%H-%M-%S")

    def _build_snapshot_filename(
        self,
        tweet_id: str,
        tweet_type: str,
        now: Optional[datetime] = None,
    ) -> str:
        """Build readable snapshot filename with stable tweet_id suffix."""
        date_str, time_str = self._jalali_tehran_parts(now=now)
        kind = self._tweet_kind(tweet_type)
        return f"{kind}_snapshot_{date_str}_{time_str}_tehran__tweet_{tweet_id}.json"

    def _build_viral_filename(
        self,
        tweet_id: str,
        tweet_type: str,
        confirmed: bool,
        now: Optional[datetime] = None,
        extension: str = ".json",
    ) -> str:
        """Build readable viral filename with classification + tweet_id suffix."""
        date_str, time_str = self._jalali_tehran_parts(now=now)
        classification = "viral_confirmed" if confirmed else "viral_candidate"
        kind = self._tweet_kind(tweet_type)
        return f"{classification}_{kind}_{date_str}_{time_str}_tehran__tweet_{tweet_id}{extension}"

    def _build_readable_filename(
        self,
        tweet_id: str,
        username: str,
        tweet_type: str,
        tweet_text: str,
        now: Optional[datetime] = None,
        extension: str = ".json",
    ) -> str:
        current = now or datetime.now(self.tz)
        jalali_now = jdatetime.datetime.fromgregorian(datetime=current)
        date_str = jalali_now.strftime("%Y-%m-%d")
        time_str = jalali_now.strftime("%H-%M-%S")
        type_str = self._safe_type(tweet_type)
        slug = self._safe_slug(tweet_text)
        tail = f"{date_str}_{time_str}_IRST_{type_str}_{slug}_{tweet_id}{extension}"
        if len(tail) <= 170:
            return tail
        overshoot = len(tail) - 170
        trimmed_slug = slug[:-overshoot] if overshoot < len(slug) else "tweet"
        trimmed_slug = trimmed_slug.rstrip("_") or "tweet"
        return f"{date_str}_{time_str}_IRST_{type_str}_{trimmed_slug}_{tweet_id}{extension}"

    def _snapshot_path_for_tweet(self, tweet_id: str, tweet: Optional[Dict] = None) -> Path:
        """Resolve snapshot path, preserving existing indexed paths if present."""
        existing_rel = self.snapshot_index.get(tweet_id)
        if existing_rel:
            existing_path = self.data_dir / existing_rel
            if existing_path.exists():
                return existing_path

        legacy_file = self.snapshots_dir / f"{tweet_id}.json"
        username = self._safe_username((tweet or {}).get("account", "unknown"))
        tweet_type = (tweet or {}).get("type", "TWEET")
        account_dir = self.snapshots_dir / username
        account_dir.mkdir(parents=True, exist_ok=True)
        new_filename = self._build_snapshot_filename(tweet_id, tweet_type)
        new_path = account_dir / new_filename

        if legacy_file.exists():
            try:
                legacy_file.rename(new_path)
                rel = str(new_path.relative_to(self.data_dir))
                self.snapshot_index[tweet_id] = rel
                self._save_snapshot_index()
                return new_path
            except Exception:
                return legacy_file

        rel = str(new_path.relative_to(self.data_dir))
        self.snapshot_index[tweet_id] = rel
        self._save_snapshot_index()
        return new_path

    def register_tweet(self, tweet_id: str, account: str, stored_in: List[str]):
        """Register a tweet in the global dedupe registry"""
        now = datetime.now(self.tz).isoformat()

        if tweet_id not in self.seen_tweets:
            self.seen_tweets[tweet_id] = {
                "first_seen": now,
                "accounts": [account],
                "stored_in": stored_in,
            }
        else:
            if account not in self.seen_tweets[tweet_id]["accounts"]:
                self.seen_tweets[tweet_id]["accounts"].append(account)
            for location in stored_in:
                if location not in self.seen_tweets[tweet_id]["stored_in"]:
                    self.seen_tweets[tweet_id]["stored_in"].append(location)

        self._save_seen_tweets()

    def is_tweet_seen(self, tweet_id: str) -> bool:
        """Check if tweet has been seen before"""
        return tweet_id in self.seen_tweets

    def save_snapshot(self, tweet_id: str, metrics: Dict, tweet: Optional[Dict] = None, force: bool = False):
        """
        Save engagement snapshot with smart delta/time filtering.

        Args:
            force: If True, bypass filtering and always save.
        """
        return self._save_snapshot_internal(tweet_id, metrics, tweet=tweet, force=force)

    def should_save_snapshot(self, tweet_id: str, new_metrics: Dict) -> Tuple[bool, str]:
        """
        Check if new snapshot differs enough from last snapshot.
        Returns: (should_save, reason)
        """
        snapshots = self.load_snapshots(tweet_id)
        if not snapshots:
            return True, "first_snapshot"

        last_snapshot = snapshots[-1]
        last_metrics = last_snapshot.get("metrics")
        if not isinstance(last_metrics, dict):
            last_metrics = {
                "likes": last_snapshot.get("likes", 0),
                "retweets": last_snapshot.get("retweets", 0),
                "replies": last_snapshot.get("replies", 0),
                "views": last_snapshot.get("views", 0),
                "quotes": last_snapshot.get("quotes", 0),
                "bookmarks": last_snapshot.get("bookmarks", 0),
            }

        try:
            last_time = datetime.fromisoformat(last_snapshot["timestamp"])
        except Exception:
            last_time = datetime.now(self.tz)

        now = datetime.now(self.tz)
        if last_time.tzinfo is None:
            last_time = self.tz.localize(last_time)
        time_delta = (now - last_time).total_seconds() / 60.0

        viral_cfg = self.config.get("viral_detection", self.config.get("viral_config", {}))
        min_time = viral_cfg.get("min_time_between_snapshots_minutes", 5)
        if time_delta < min_time:
            return False, f"too_soon_{time_delta:.1f}min"

        thresholds = viral_cfg.get("snapshot_delta_threshold", {
            "likes": 5,
            "retweets": 2,
            "replies": 2,
            "views": 100,
            "quotes": 1,
            "bookmarks": 3,
        })

        for metric, threshold in thresholds.items():
            old_val = last_metrics.get(metric, 0) or 0
            new_val = new_metrics.get(metric, 0) or 0
            if isinstance(old_val, str):
                old_val = 0 if old_val.lower() == "unknown" else int(old_val) if old_val.isdigit() else 0
            if isinstance(new_val, str):
                new_val = 0 if new_val.lower() == "unknown" else int(new_val) if new_val.isdigit() else 0
            delta = abs(new_val - old_val)
            if delta >= threshold:
                return True, f"{metric}_changed_by_{delta}"

        return False, "no_significant_change"

    def _save_snapshot_internal(
        self,
        tweet_id: str,
        metrics: Dict,
        tweet: Optional[Dict] = None,
        force: bool = False,
    ) -> bool:
        """Internal snapshot saver with smart filtering support."""
        if not force:
            should_save, reason = self.should_save_snapshot(tweet_id, metrics)
            if not should_save:
                print(f"[SKIP] {tweet_id}: {reason}")
                return False
            print(f"[SAVE] {tweet_id}: {reason}")

        snapshot_file = self._snapshot_path_for_tweet(tweet_id, tweet=tweet)

        snapshots = []
        if snapshot_file.exists():
            try:
                with open(snapshot_file) as f:
                    snapshots = json.load(f)
            except Exception:
                pass

        snapshot = {
            "timestamp": datetime.now(self.tz).isoformat(),
            "metrics": metrics,
            **metrics,
        }
        snapshots.append(snapshot)

        with open(snapshot_file, "w") as f:
            json.dump(snapshots, f, indent=2)
        return True

    def load_snapshots(self, tweet_id: str) -> List[Dict]:
        """Load all snapshots for a tweet"""
        candidate_paths = []

        indexed_rel = self.snapshot_index.get(tweet_id)
        if indexed_rel:
            candidate_paths.append(self.data_dir / indexed_rel)

        candidate_paths.append(self.snapshots_dir / f"{tweet_id}.json")

        if not indexed_rel:
            for path in self.snapshots_dir.glob(f"*/*_{tweet_id}.json"):
                candidate_paths.append(path)

        for snapshot_file in candidate_paths:
            if not snapshot_file.exists():
                continue
            try:
                with open(snapshot_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    if snapshot_file.is_relative_to(self.data_dir):
                        self.snapshot_index[tweet_id] = str(snapshot_file.relative_to(self.data_dir))
                        self._save_snapshot_index()
                    return data
            except Exception:
                pass
        return []

    def compare_endpoints(
        self,
        tweets_only: List[Dict],
        tweets_and_replies: List[Dict],
    ) -> Tuple[List[Dict], List[Dict], List[Dict], List[Dict]]:
        """
        Compare two endpoint outputs and categorize tweets

        Returns:
            (tweets_only_list, tweets_and_replies_list, merged_list, diffs_list)
        """
        tweets_only_ids = {t["id"] for t in tweets_only}
        tweets_and_replies_ids = {t["id"] for t in tweets_and_replies}
        all_tweets = {t["id"]: t for t in tweets_only + tweets_and_replies}

        merged_ids = tweets_only_ids & tweets_and_replies_ids
        merged_list = [all_tweets[tid] for tid in merged_ids]

        only_in_tweets = tweets_only_ids - tweets_and_replies_ids
        only_in_replies = tweets_and_replies_ids - tweets_only_ids

        diffs_list = []
        for tid in only_in_tweets:
            tweet = all_tweets[tid].copy()
            tweet["source_endpoint"] = "UserTweets"
            diffs_list.append(tweet)
        for tid in only_in_replies:
            tweet = all_tweets[tid].copy()
            tweet["source_endpoint"] = "UserTweetsAndReplies"
            diffs_list.append(tweet)

        return tweets_only, tweets_and_replies, merged_list, diffs_list

    def save_tweets_to_file(
        self,
        tweets: List[Dict],
        account: str,
        date_str: str,
        output_dir: Path,
        append: bool = False,
    ):
        """Save tweets to a daily file"""
        account_dir = output_dir / account.upper()
        account_dir.mkdir(parents=True, exist_ok=True)
        output_file = account_dir / f"{date_str}.txt"

        existing_ids = set()
        if append and output_file.exists():
            try:
                with open(output_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    existing_ids = set(re.findall(r"https://x\.com/\w+/status/(\d+)", content))
            except Exception:
                pass

        new_tweets = [tweet for tweet in tweets if tweet["id"] not in existing_ids]
        if not new_tweets:
            return 0

        formatted = self._format_tweets(new_tweets)

        mode = "a" if append else "w"
        with open(output_file, mode, encoding="utf-8") as f:
            if append and output_file.stat().st_size > 0:
                f.write("\n\n" + ("═" * 70) + "\n\n")
            f.write(formatted)

        return len(new_tweets)

    def _format_tweets(self, tweets: List[Dict]) -> str:
        """Format tweets for text output"""
        sections = [self._format_item(tweet) for tweet in tweets]
        return "".join(sections)

    def _format_count(self, value) -> str:
        """Format numeric count values safely"""
        if value is None or value == "unknown":
            return "UNKNOWN"
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return str(value).upper() if str(value).lower() == "unknown" else str(value)

    def _wrap(self, text: str, width: int = 70) -> str:
        """Wrap long text to readable width."""
        lines = []
        for para in str(text).split("\n"):
            if para:
                lines.append(
                    textwrap.fill(
                        para, width=width, break_long_words=False, break_on_hyphens=False
                    )
                )
            else:
                lines.append("")
        return "\n".join(lines)

    def _format_entities(self, tweet: Dict) -> List[str]:
        """Format URLs, hashtags, mentions, and media from parsed entities."""
        lines = []

        entities = tweet.get("entities", {}) or {}
        urls = [entry.get("expanded") or entry.get("short") for entry in entities.get("urls", []) if (entry.get("expanded") or entry.get("short"))]
        if not urls:
            text = str(tweet.get("text") or "")
            urls = sorted(set(re.findall(r"https?://\S+", text)))

        if urls:
            lines.append("🔗 Links:")
            for url in urls:
                lines.append(f"   → {url}")
            lines.append("")

        media_links = entities.get("media_links", [])
        if media_links:
            media_types = entities.get("media_types", [])
            media_type_suffix = f" ({', '.join(media_types)})" if media_types else ""
            lines.append(f"📎 Media: {len(media_links)} item(s){media_type_suffix}")
            for media_url in media_links:
                lines.append(f"   → {media_url}")
            lines.append("")

        hashtags = entities.get("hashtags", [])
        if hashtags:
            lines.append("🏷️  " + " ".join(f"#{tag}" for tag in hashtags))
            lines.append("")

        mentions = entities.get("mentions", [])
        if mentions:
            lines.append("👥 Mentions:")
            for mention in mentions:
                handle = mention.get("handle", "")
                name = mention.get("name", "")
                lines.append(f"   @{handle} ({name})".strip())
            lines.append("")

        return lines

    def _metric_line(self, source: Dict) -> str:
        return (
            f"💬 Replies: {self._format_count(source.get('replies'))}  "
            f"🔁 Retweets: {self._format_count(source.get('retweets'))}  "
            f"❤️ Likes: {self._format_count(source.get('likes'))}  "
            f"💭 Quotes: {self._format_count(source.get('quotes'))}  "
            f"🔖 Bookmarks: {self._format_count(source.get('bookmarks'))}  "
            f"👁 Views: {self._format_count(source.get('views'))}"
        )

    def _get_type_icon(self, tweet_type: str) -> str:
        """Return icon for tweet type"""
        mapping = {
            "Tweet": "📝",
            "Retweet": "🔁",
            "Reply": "💬",
            "Quote": "💭",
        }
        return mapping.get(tweet_type, "📝")

    def _format_tweet(self, tweet: Dict) -> str:
        text_payload = choose_export_text(
            tweet.get("text", ""),
            tweet.get("source_language"),
            tweet.get("translation_meta"),
        )
        lines = ["💬 TWEET", ""]
        lines.append(f"👤 @{tweet.get('account', 'unknown')}")
        lines.append(f"📅 {tweet.get('timestamp', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._wrap(text_payload["text"]))
        if text_payload.get("note"):
            lines.append("")
            lines.append(text_payload["note"])
        lines.append("")
        lines.extend(self._format_entities(tweet))
        lines.append(f"🔗 {tweet.get('url', '')}")
        lines.append(f"🆔 Tweet ID: {tweet.get('id', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._metric_line(tweet))
        if "source_endpoint" in tweet:
            lines.extend(["", f"Source Endpoint: {tweet['source_endpoint']}"])
        lines.extend(["", "═" * 70, "═" * 70, ""])
        return "\n".join(lines)

    def _format_retweet(self, tweet: Dict) -> str:
        original_export = choose_export_text(
            tweet.get("retweeted_text") or tweet.get("text", ""),
            None,
            tweet.get("retweeted_translation_meta"),
        )
        lines = ["🔁 RETWEET", ""]
        lines.append(f"👤 Retweeted by: @{tweet.get('account', 'unknown')}")
        lines.append(f"📅 Retweeted at: {tweet.get('timestamp', 'UNKNOWN')}")
        lines.append("")
        lines.append("┌" + "─" * 68 + "┐")
        lines.append("│ ORIGINAL TWEET" + " " * 53 + "│")
        lines.append("├" + "─" * 68 + "┤")

        original_author = tweet.get("retweeted_author")
        original_tweet_id = tweet.get("retweeted_tweet_id")
        original_author_display = f"@{original_author}" if original_author else "UNKNOWN"
        author_line = f"👤 {original_author_display}"
        lines.append(f"│ {author_line}" + " " * max(0, 68 - len(f"│ {author_line}")) + "│")
        lines.append("│" + " " * 68 + "│")

        text = self._wrap(original_export["text"])
        for line in text.split("\n"):
            for wrapped in textwrap.wrap(line, width=66) if line else [""]:
                lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
        if original_export.get("note"):
            lines.append("│" + " " * 68 + "│")
            note_text = original_export["note"]
            for wrapped in textwrap.wrap(note_text, width=66):
                lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
        lines.append("│" + " " * 68 + "│")

        if tweet.get("retweeted_timestamp"):
            time_line = f"📅 {tweet['retweeted_timestamp']}"
            lines.append(f"│ {time_line}" + " " * max(0, 68 - len(f"│ {time_line}")) + "│")
            lines.append("│" + " " * 68 + "│")

        if original_tweet_id:
            original_url = f"https://x.com/{original_author or 'i'}/status/{original_tweet_id}"
            link_line = f"🔗 {original_url}"
            id_line = f"🆔 Tweet ID: {original_tweet_id}"
            lines.append(f"│ {link_line}" + " " * max(0, 68 - len(f"│ {link_line}")) + "│")
            lines.append(f"│ {id_line}" + " " * max(0, 68 - len(f"│ {id_line}")) + "│")
        else:
            lines.append("│ 🔗 UNKNOWN" + " " * 57 + "│")
            lines.append("│ 🆔 Tweet ID: UNKNOWN" + " " * 46 + "│")

        engagement_line = self._metric_line(tweet)
        lines.append("│" + " " * 68 + "│")
        lines.append(f"│ {engagement_line}" + " " * max(0, 68 - len(f"│ {engagement_line}")) + "│")
        lines.append("└" + "─" * 68 + "┘")

        if "source_endpoint" in tweet:
            lines.extend(["", f"Source Endpoint: {tweet['source_endpoint']}"])
        lines.extend(["", "═" * 70, "═" * 70, ""])
        return "\n".join(lines)

    def _format_reply(self, tweet: Dict) -> str:
        reply_text_payload = choose_export_text(
            tweet.get("text", ""),
            tweet.get("source_language"),
            tweet.get("translation_meta"),
        )
        lines = ["↩️ REPLY", ""]
        lines.append(f"👤 Reply by: @{tweet.get('account', 'unknown')}")
        lines.append(f"📅 {tweet.get('timestamp', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._wrap(reply_text_payload["text"]))
        if reply_text_payload.get("note"):
            lines.append("")
            lines.append(reply_text_payload["note"])
        lines.append("")
        lines.extend(self._format_entities(tweet))
        lines.append(f"🔗 {tweet.get('url', '')}")
        lines.append(f"🆔 Tweet ID: {tweet.get('id', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._metric_line(tweet))
        lines.append("")

        parent_id = tweet.get("in_reply_to_status_id")
        parent_screen = tweet.get("in_reply_to_screen_name")
        parent_tweet = tweet.get("parent_tweet", {})
        chain = tweet.get("conversation_chain", [])
        if chain:
            lines.append("┌" + "─" * 68 + "┐")
            lines.append("│ CONVERSATION THREAD" + " " * 48 + "│")
            lines.append("├" + "─" * 68 + "┤")
            for idx, node in enumerate(chain, start=1):
                role = "Direct parent" if idx == len(chain) else f"Ancestor {idx}"
                author_line = f"{role}: @{node.get('account', 'unknown')}"
                lines.append(f"│ {author_line}" + " " * max(0, 68 - len(f"│ {author_line}")) + "│")
                if node.get("timestamp"):
                    time_line = f"📅 {node['timestamp']}"
                    lines.append(f"│ {time_line}" + " " * max(0, 68 - len(f"│ {time_line}")) + "│")
                node_text_payload = choose_export_text(
                    node.get("text", ""),
                    node.get("source_language"),
                    node.get("translation_meta"),
                )
                text_value = self._wrap(node_text_payload["text"])
                for line in text_value.split("\n"):
                    for wrapped in textwrap.wrap(line, width=66) if line else [""]:
                        lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
                if node_text_payload.get("note"):
                    lines.append("│" + " " * 68 + "│")
                    for wrapped in textwrap.wrap(node_text_payload["note"], width=66):
                        lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
                if node.get("url"):
                    link_line = f"🔗 {node['url']}"
                    lines.append(f"│ {link_line}" + " " * max(0, 68 - len(f"│ {link_line}")) + "│")
                if node.get("id"):
                    id_line = f"🆔 Tweet ID: {node['id']}"
                    lines.append(f"│ {id_line}" + " " * max(0, 68 - len(f"│ {id_line}")) + "│")
                if idx != len(chain):
                    lines.append("│" + " " * 68 + "│")
                    lines.append("│ replies to" + " " * 58 + "│")
                    lines.append("│" + " " * 68 + "│")
            lines.append("└" + "─" * 68 + "┘")
            lines.append("")

        lines.append("┌" + "─" * 68 + "┐")
        lines.append("│ IN REPLY TO" + " " * 56 + "│")
        lines.append("├" + "─" * 68 + "┤")
        parent_label = f"@{parent_screen}" if parent_screen else "UNKNOWN"
        if parent_tweet:
            parent_label = f"@{parent_tweet.get('account', parent_label).lstrip('@')}"
        label_line = f"👤 {parent_label}"
        lines.append(f"│ {label_line}" + " " * max(0, 68 - len(f"│ {label_line}")) + "│")
        if tweet.get("conversation_id"):
            conv_line = f"🧵 Conversation ID: {tweet['conversation_id']}"
            lines.append(f"│ {conv_line}" + " " * max(0, 68 - len(f"│ {conv_line}")) + "│")
        lines.append("│" + " " * 68 + "│")

        if parent_tweet.get("text"):
            parent_text_payload = choose_export_text(
                parent_tweet.get("text", ""),
                parent_tweet.get("source_language"),
                parent_tweet.get("translation_meta"),
            )
            parent_text = self._wrap(parent_text_payload["text"])
            for line in parent_text.split("\n"):
                for wrapped in textwrap.wrap(line, width=66) if line else [""]:
                    lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
            if parent_text_payload.get("note"):
                lines.append("│" + " " * 68 + "│")
                for wrapped in textwrap.wrap(parent_text_payload["note"], width=66):
                    lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
            lines.append("│" + " " * 68 + "│")

        if parent_id:
            parent_url = parent_tweet.get("url") or f"https://x.com/{parent_screen or 'i'}/status/{parent_id}"
            link_line = f"🔗 {parent_url}"
            id_line = f"🆔 Tweet ID: {parent_id}"
            lines.append(f"│ {link_line}" + " " * max(0, 68 - len(f"│ {link_line}")) + "│")
            lines.append(f"│ {id_line}" + " " * max(0, 68 - len(f"│ {id_line}")) + "│")
        else:
            lines.append("│ 🔗 UNKNOWN" + " " * 57 + "│")
            lines.append("│ 🆔 Tweet ID: UNKNOWN" + " " * 46 + "│")
        lines.append("└" + "─" * 68 + "┘")

        if "source_endpoint" in tweet:
            lines.extend(["", f"Source Endpoint: {tweet['source_endpoint']}"])
        lines.extend(["", "═" * 70, "═" * 70, ""])
        return "\n".join(lines)

    def _format_quote(self, tweet: Dict) -> str:
        quote_text_payload = choose_export_text(
            tweet.get("text", ""),
            tweet.get("source_language"),
            tweet.get("translation_meta"),
        )
        lines = ["📎 QUOTE TWEET", ""]
        lines.append(f"👤 Quote by: @{tweet.get('account', 'unknown')}")
        lines.append(f"📅 {tweet.get('timestamp', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._wrap(quote_text_payload["text"]))
        if quote_text_payload.get("note"):
            lines.append("")
            lines.append(quote_text_payload["note"])
        lines.append("")
        lines.extend(self._format_entities(tweet))
        lines.append(f"🔗 {tweet.get('url', '')}")
        lines.append(f"🆔 Tweet ID: {tweet.get('id', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._metric_line(tweet))
        lines.append("")
        lines.append("┌" + "─" * 68 + "┐")
        lines.append("│ QUOTED TWEET" + " " * 55 + "│")
        lines.append("├" + "─" * 68 + "┤")
        quoted_author = tweet.get("quoted_author")
        quoted_id = tweet.get("quoted_tweet_id")
        if quoted_author:
            author_line = f"👤 @{quoted_author}"
            lines.append(f"│ {author_line}" + " " * max(0, 68 - len(f"│ {author_line}")) + "│")
        else:
            lines.append("│ 👤 UNKNOWN" + " " * 57 + "│")
        lines.append("│" + " " * 68 + "│")

        if tweet.get("quoted_timestamp"):
            time_line = f"📅 {tweet['quoted_timestamp']}"
            lines.append(f"│ {time_line}" + " " * max(0, 68 - len(f"│ {time_line}")) + "│")
            lines.append("│" + " " * 68 + "│")

        if tweet.get("quoted_text"):
            quoted_text_payload = choose_export_text(
                tweet.get("quoted_text", ""),
                None,
                tweet.get("quoted_translation_meta"),
            )
            quoted_text = self._wrap(quoted_text_payload["text"])
            for line in quoted_text.split("\n"):
                for wrapped in textwrap.wrap(line, width=66) if line else [""]:
                    lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
            if quoted_text_payload.get("note"):
                lines.append("│" + " " * 68 + "│")
                for wrapped in textwrap.wrap(quoted_text_payload["note"], width=66):
                    lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
            lines.append("│" + " " * 68 + "│")

        if quoted_id:
            quoted_url = f"https://x.com/{quoted_author or 'i'}/status/{quoted_id}"
            link_line = f"🔗 {quoted_url}"
            id_line = f"🆔 Tweet ID: {quoted_id}"
            lines.append(f"│ {link_line}" + " " * max(0, 68 - len(f"│ {link_line}")) + "│")
            lines.append(f"│ {id_line}" + " " * max(0, 68 - len(f"│ {id_line}")) + "│")
        else:
            lines.append("│ 🔗 UNKNOWN" + " " * 57 + "│")
            lines.append("│ 🆔 Tweet ID: UNKNOWN" + " " * 46 + "│")
        lines.append("└" + "─" * 68 + "┘")
        if "source_endpoint" in tweet:
            lines.extend(["", f"Source Endpoint: {tweet['source_endpoint']}"])
        lines.extend(["", "═" * 70, "═" * 70, ""])
        return "\n".join(lines)

    def _format_item(self, tweet: Dict) -> str:
        """Format tweet by type using legacy-style layout."""
        tweet_type = (tweet.get("type") or "Tweet").lower()
        if tweet_type == "retweet":
            return self._format_retweet(tweet)
        if tweet_type == "reply":
            return self._format_reply(tweet)
        if tweet_type == "quote":
            return self._format_quote(tweet)
        return self._format_tweet(tweet)

    def save_viral_report(
        self,
        tweet: Dict,
        metrics: Dict,
        velocity: Dict,
        classification: str,
        confirmed: bool = False,
    ):
        """Save readable and machine-parseable viral report."""
        target_dir = self.viral_confirmed_dir if confirmed else self.viral_candidates_dir
        account = self._safe_username(tweet.get("account", "unknown"))
        account_dir = target_dir / account
        account_dir.mkdir(parents=True, exist_ok=True)

        tweet_id = str(tweet.get("id", "unknown"))
        tweet_type = str(tweet.get("type", "TWEET"))
        output_file = account_dir / self._build_viral_filename(
            tweet_id=tweet_id,
            tweet_type=tweet_type,
            confirmed=confirmed,
            extension=".json",
        )
        report_file = account_dir / self._build_viral_filename(
            tweet_id=tweet_id,
            tweet_type=tweet_type,
            confirmed=confirmed,
            extension=".txt",
        )

        report = self._format_viral_report(tweet, metrics, velocity, classification)
        now = datetime.now(self.tz)
        payload = {
            "generated_at": now.isoformat(),
            "generated_at_jalali": self.get_jalali_datetime(now),
            "account": tweet.get("account", "unknown"),
            "classification": classification,
            "confirmed": confirmed,
            "tweet_id": str(tweet_id),
            "tweet_type": tweet.get("type", "Tweet"),
            "tweet": tweet,
            "metrics": metrics,
            "velocity": velocity,
            "report_text": report,
        }
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        with open(report_file, "w", encoding="utf-8") as f:
            f.write(report)

    def _format_viral_report(
        self,
        tweet: Dict,
        metrics: Dict,
        velocity: Dict,
        classification: str,
    ) -> str:
        """Format beautiful viral report"""
        account = tweet.get("account", "Unknown")
        tweet_type = tweet.get("type", "Tweet")
        url = tweet.get("url", "")
        timestamp = tweet.get("timestamp", "")
        text_payload = choose_export_text(
            tweet.get("text", ""),
            tweet.get("source_language"),
            tweet.get("translation_meta"),
        )

        lines = [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "🔥 VIRAL ALERT",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            "🧾 Summary",
            f"Account: @{account}",
            f"Type: {tweet_type}",
            f"Classification: {classification}",
        ]

        if url:
            lines.append(f"Tweet URL: {url}")
        if timestamp:
            lines.append(f"Posted: {timestamp}")

        lines.extend(
            [
                "",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "📝 Text",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "",
            ]
        )

        text = str(text_payload.get("text", "") or "").strip()
        if text:
            preview = text[:200] + "..." if len(text) > 200 else text
            lines.append(preview)
        else:
            lines.append("No text available")
        if text_payload.get("note"):
            lines.extend(["", text_payload["note"]])

        lines.extend(
            [
                "",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "📊 Current Metrics",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "",
            ]
        )

        likes = metrics.get("likes", 0)
        retweets = metrics.get("retweets", 0)
        replies = metrics.get("replies", 0)
        views = metrics.get("views", 0)
        if likes != "unknown" and likes is not None:
            lines.append(f"❤️  Likes: {likes:,}")
        if retweets != "unknown" and retweets is not None:
            lines.append(f"🔁 Retweets: {retweets:,}")
        if replies != "unknown" and replies is not None:
            lines.append(f"💬 Replies: {replies:,}")
        if views != "unknown" and views is not None:
            lines.append(f"👁  Views: {views:,}")

        lines.extend(
            [
                "",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "📈 Growth Velocity",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "",
            ]
        )

        likes_per_min = velocity.get("likes_per_min", 0)
        retweets_per_min = velocity.get("retweets_per_min", 0)
        views_per_min = velocity.get("views_per_min", 0)
        acceleration = velocity.get("acceleration", 0)
        if likes_per_min:
            lines.append(f"↑ Likes/min: +{likes_per_min:,.1f}")
        if retweets_per_min:
            lines.append(f"↑ RTs/min: +{retweets_per_min:,.1f}")
        if views_per_min:
            lines.append(f"↑ Views/min: +{views_per_min:,.1f}")
        if acceleration:
            lines.extend(["", "Acceleration:", f"+{acceleration:.1f}% in recent interval"])

        lines.extend(["", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"])
        return "\n".join(lines)

    def log_event(self, log_type: str, message: str):
        """Log an event to appropriate log file"""
        log_file = self.logs_dir / f"{log_type}.log"
        timestamp = datetime.now(self.tz).isoformat()
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")

    def get_jalali_date(self, dt: Optional[datetime] = None) -> str:
        """Convert datetime to Jalali date string"""
        if dt is None:
            dt = datetime.now(self.tz)
        jalali = jdatetime.datetime.fromgregorian(datetime=dt)
        return jalali.strftime("%Y-%m-%d")

    def get_jalali_datetime(self, dt: Optional[datetime] = None) -> str:
        """Convert datetime to Jalali datetime string"""
        if dt is None:
            dt = datetime.now(self.tz)
        jalali = jdatetime.datetime.fromgregorian(datetime=dt)
        return jalali.strftime("%Y-%m-%d %H:%M:%S")
