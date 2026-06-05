#!/usr/bin/env python3
"""
Twitter Historical Tweet Fetcher - Hybrid

Uses api_manager.py and storage_manager.py for clean separation of concerns.

Key improvements:
- Fetch once, distribute to 4 folders (USER_TWEETS, USER_TWEETS_AND_REPLIES, MERGED_TIMELINES, ENDPOINT_DIFFS)
- Global dedupe registry
- Per-endpoint rate limiting
- Endpoint health monitoring
- Graceful degradation (404 doesn't crash pipeline)
- Structured logging
"""

import json
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Import new managers
from api_manager import APIManager
from storage_manager import StorageManager, extract_metrics
from tier_config import get_priority_policy, load_tier_config, ordered_accounts
from text_export_helper import extract_translation_meta

try:
    import jdatetime
    import pytz
except ImportError:
    print("ERROR: Missing dependencies. Run: pip3 install jdatetime pytz")
    sys.exit(1)

# ============================================================================
# CONFIGURATION
# ============================================================================

# Leave empty to use all accounts from tier configuration.
ACCOUNTS: List[str] = []

# Default fallback when no tier policy is found.
HISTORY_DAYS = 3

# Fetch replies endpoint (set to False if getting 404s)
FETCH_REPLIES = True

# Timezone
TIMEZONE = "Asia/Tehran"

# Random pause between requests (seconds)
MIN_DELAY = 2
MAX_DELAY = 5

# Fallback max pages when policy data is missing.
DEFAULT_HISTORICAL_MAX_PAGES = 15

SEP = "═" * 70


# ============================================================================
# MAIN FETCHER CLASS
# ============================================================================

class TwitterHistoricalFetcher:
    """Fetches historical tweets using new architecture"""
    
    def __init__(self, config_path: str = "config.json"):
        self.base_dir = Path(__file__).parent
        
        # Initialize managers
        print("Initializing managers...")
        self.api_manager = APIManager(config_path, state_dir=self.base_dir / "data" / "STATE")
        self.storage_manager = StorageManager(self.base_dir, timezone=TIMEZONE)
        
        self.config = self.api_manager.config
        self.tz = pytz.timezone(TIMEZONE)
        self.account_map, self.priority_policies = load_tier_config(self.config)
        self.active_history_days = HISTORY_DAYS
        
        print("✓ Managers initialized")

    def _build_timeline_progress_state(self) -> Dict:
        """State tracker for cursor lifecycle and API waste protection."""
        return {
            "seen_cursor_values": set(),
            "cursor_attempt_counts": {},
            "cursor_generation_history": [],
            "blacklisted_cursors": set(),
            "active_cursor_reseeded": False,
            "last_new_tweet_total": 0,
            "no_progress_pages": 0,
            "had_progress_before_stall": False,
        }

    def _note_cursor_attempt(self, state: Dict, cursor_value: Optional[str]) -> bool:
        """
        Track attempts per cursor and enforce max 2 attempts.
        Returns True when cursor is still allowed, False when exhausted.
        """
        if not cursor_value:
            return True
        attempts = int(state["cursor_attempt_counts"].get(cursor_value, 0)) + 1
        state["cursor_attempt_counts"][cursor_value] = attempts
        if attempts > 2:
            state["blacklisted_cursors"].add(cursor_value)
            return False
        return True

    def _can_use_cursor(self, state: Dict, cursor_value: Optional[str]) -> bool:
        """Check blacklist + attempt budget for cursor value."""
        if not cursor_value:
            return True
        if cursor_value in state["blacklisted_cursors"]:
            return False
        return self._note_cursor_attempt(state, cursor_value)

    def _should_try_single_reseed(self, state: Dict, cursor_value: Optional[str]) -> bool:
        """
        Allow one reseed only if prior pages showed progression.
        Prevents endless cursor=None loops.
        """
        if not cursor_value:
            return False
        if state["active_cursor_reseeded"]:
            return False
        if not state["had_progress_before_stall"]:
            return False
        state["active_cursor_reseeded"] = True
        return True

    def _parse_timeline_page(
        self,
        instructions: List[Dict],
        seen_ids: Set[str],
        include_conversation_modules: bool = False,
    ) -> Dict:
        """Parse timeline page and return progression signals + parsed tweets."""
        page_tweets: List[Dict] = []
        next_cursor: Optional[str] = None
        timeline_item_count = 0
        timeline_module_count = 0
        has_entries = False
        reached_time_limit = False

        for inst in instructions:
            if inst.get("type") != "TimelineAddEntries":
                continue

            entries = inst.get("entries", [])
            if entries:
                has_entries = True

            for entry in entries:
                entry_id = entry.get("entryId", "")

                if entry_id.startswith("cursor-bottom-"):
                    next_cursor = entry.get("content", {}).get("value")
                    continue

                if entry_id.startswith("tweet-"):
                    timeline_item_count += 1
                    content = entry.get("content", {})
                    item_content = content.get("itemContent", {})
                    tweet_results = item_content.get("tweet_results", {})
                    tweet_obj = self._unwrap_tweet_result(tweet_results)
                    if not tweet_obj:
                        continue

                    parsed = self._parse_tweet(tweet_obj)
                    if not parsed:
                        continue

                    if not self._is_within_timeframe(parsed.get("raw_timestamp") or parsed["timestamp"]):
                        reached_time_limit = True
                        next_cursor = None
                        break

                    if parsed["id"] not in seen_ids:
                        seen_ids.add(parsed["id"])
                        page_tweets.append(parsed)
                    continue

                if include_conversation_modules and entry_id.startswith("profile-conversation-"):
                    timeline_module_count += 1
                    content = entry.get("content", {})
                    for item_entry in content.get("items", []):
                        item_content = item_entry.get("item", {}).get("itemContent", {})
                        tweet_results = item_content.get("tweet_results", {})
                        tweet_obj = self._unwrap_tweet_result(tweet_results)
                        if not tweet_obj:
                            continue
                        parsed = self._parse_tweet(tweet_obj)
                        if not parsed:
                            continue
                        if not self._is_within_timeframe(parsed.get("raw_timestamp") or parsed["timestamp"]):
                            continue
                        if parsed["id"] not in seen_ids:
                            seen_ids.add(parsed["id"])
                            page_tweets.append(parsed)

            if reached_time_limit:
                break

        return {
            "tweets": page_tweets,
            "next_cursor": next_cursor,
            "has_entries": has_entries,
            "timeline_item_count": timeline_item_count,
            "timeline_module_count": timeline_module_count,
            "reached_time_limit": reached_time_limit,
        }

    def _classify_stall_reason(
        self,
        *,
        cursor: Optional[str],
        next_cursor: Optional[str],
        has_entries: bool,
        timeline_item_count: int,
        timeline_module_count: int,
        new_items_count: int,
    ) -> Optional[str]:
        """Return normalized stall reason or None if pagination is healthy."""
        if not has_entries:
            return "empty_entries"
        if timeline_item_count == 0 and timeline_module_count == 0:
            return "no_timeline_items_or_modules"
        if cursor and next_cursor and next_cursor == cursor:
            return "repeated_cursor_detected"
        if cursor and new_items_count == 0:
            return "no_new_tweets_detected"
        if cursor and not next_cursor:
            return "no_bottom_cursor"
        return None
    
    def _compact_json(self, payload: Dict) -> str:
        """Compact JSON for URL params"""
        return json.dumps(payload, separators=(",", ":"))

    def _random_human_pause(self, bucket: str = "between_pages"):
        """Random pause using anti-bot simulation config."""
        sim = self.config.get("anti_bot_simulation", {})
        if not sim.get("enabled", True):
            return
        delay_cfg = sim.get("delays_seconds", {})
        if bucket == "between_accounts":
            min_d = float(delay_cfg.get("between_accounts_min", 3))
            max_d = float(delay_cfg.get("between_accounts_max", 6))
        elif bucket == "replies_retry":
            min_d = float(delay_cfg.get("replies_retry_min", 1))
            max_d = float(delay_cfg.get("replies_retry_max", 3))
        else:
            min_d = float(delay_cfg.get("between_pages_min", MIN_DELAY))
            max_d = float(delay_cfg.get("between_pages_max", MAX_DELAY))
        if max_d < min_d:
            max_d = min_d
        time.sleep(random.uniform(min_d, max_d))

    def _normalize_metric(self, value):
        """Normalize metrics and expose missing fields as 'unknown'."""
        if value is None:
            return "unknown"
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned == "":
                return "unknown"
            if cleaned.lower() in ["unknown", "none", "null"]:
                return "unknown"
            if cleaned.isdigit():
                return int(cleaned)
            return cleaned
        if isinstance(value, (int, float)):
            return int(value)
        return "unknown"
    
    def _is_within_timeframe(self, timestamp_str: str) -> bool:
        """Check if tweet is within our history window"""
        try:
            dt = datetime.strptime(timestamp_str, "%a %b %d %H:%M:%S %z %Y")
            cutoff = datetime.now(dt.tzinfo) - timedelta(days=self.active_history_days)
            return dt >= cutoff
        except Exception:
            pass

        try:
            dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            cutoff = datetime.now(dt.tzinfo or self.tz) - timedelta(days=self.active_history_days)
            return dt >= cutoff
        except:
            return True  # If we can't parse, include it
    
    def _parse_timestamp(self, created_at: str) -> str:
        """Convert Twitter timestamp to Jalali datetime"""
        try:
            # Twitter format: "Wed May 06 12:34:56 +0000 2026"
            dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
            dt_local = dt.astimezone(self.tz)
            return self.storage_manager.get_jalali_datetime(dt_local)
        except:
            return created_at
    
    def _extract_tweet_text(self, tweet_obj: Dict) -> str:
        """Extract full text from tweet object"""
        note_result = (
            tweet_obj.get("note_tweet", {})
            .get("note_tweet_results", {})
            .get("result", {})
        )
        note_text = note_result.get("text")
        if note_text:
            return note_text

        full_text = tweet_obj.get("legacy", {}).get("full_text", "")
        for url_obj in tweet_obj.get("legacy", {}).get("entities", {}).get("urls", []):
            short_url = url_obj.get("url", "")
            expanded_url = url_obj.get("expanded_url", short_url)
            if short_url and expanded_url:
                full_text = full_text.replace(short_url, expanded_url)
        return full_text

    def _unwrap_tweet_result(self, wrapper: Dict) -> Optional[Dict]:
        """Handle X API wrappers from timeline, retweet, quote, and detail payloads."""
        if not isinstance(wrapper, dict):
            return None

        typename = wrapper.get("__typename", "")
        if typename in ["TweetTombstone", "TweetUnavailable"]:
            return None

        result = wrapper.get("result")
        if isinstance(result, dict):
            if isinstance(result.get("tweet"), dict):
                return result["tweet"]
            return result

        if isinstance(wrapper.get("tweet"), dict):
            return wrapper["tweet"]

        if wrapper.get("__typename") in {
            "Tweet",
            "TweetWithVisibilityResults",
            "TweetTombstone",
            "TweetUnavailable",
        }:
            if isinstance(wrapper.get("tweet"), dict):
                return wrapper["tweet"]
            return wrapper

        return None

    def _timeline_features(self) -> Dict:
        """Feature payload copied from the original reliable fetcher."""
        return {
            "rweb_video_screen_enabled": False,
            "rweb_cashtags_enabled": True,
            "profile_label_improvements_pcf_label_in_post_enabled": True,
            "responsive_web_profile_redirect_enabled": False,
            "rweb_tipjar_consumption_enabled": False,
            "verified_phone_label_enabled": False,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "premium_content_api_read_enabled": False,
            "communities_web_enable_tweet_community_results_fetch": True,
            "c9s_tweet_anatomy_moderator_badge_enabled": True,
            "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
            "responsive_web_grok_analyze_post_followups_enabled": True,
            "rweb_cashtags_composer_attachment_enabled": False,
            "responsive_web_jetfuel_frame": True,
            "responsive_web_grok_share_attachment_enabled": True,
            "responsive_web_grok_annotations_enabled": True,
            "articles_preview_enabled": True,
            "responsive_web_edit_tweet_api_enabled": True,
            "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
            "view_counts_everywhere_api_enabled": True,
            "longform_notetweets_consumption_enabled": True,
            "responsive_web_twitter_article_tweet_consumption_enabled": True,
            "content_disclosure_indicator_enabled": True,
            "content_disclosure_ai_generated_indicator_enabled": True,
            "responsive_web_grok_show_grok_translated_post": True,
            "responsive_web_grok_analysis_button_from_backend": True,
            "post_ctas_fetch_enabled": True,
            "freedom_of_speech_not_reach_fetch_enabled": True,
            "standardized_nudges_misinfo": True,
            "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
            "longform_notetweets_rich_text_read_enabled": True,
            "longform_notetweets_inline_media_enabled": False,
            "responsive_web_grok_image_annotation_enabled": True,
            "responsive_web_grok_imagine_annotation_enabled": True,
            "responsive_web_grok_community_note_auto_translation_is_enabled": True,
            "responsive_web_enhance_cards_enabled": False,
        }

    def _extract_entities(self, legacy: Dict) -> Dict:
        """Extract URLs, hashtags, mentions, and media links from timeline payload."""
        entities_data = legacy.get("entities", {})

        urls = []
        for url_obj in entities_data.get("urls", []):
            urls.append({
                "display": url_obj.get("display_url", ""),
                "expanded": url_obj.get("expanded_url", ""),
                "short": url_obj.get("url", ""),
            })

        hashtags = [tag.get("text", "") for tag in entities_data.get("hashtags", []) if tag.get("text")]

        mentions = []
        for mention in entities_data.get("user_mentions", []):
            mentions.append({
                "name": mention.get("name", ""),
                "handle": mention.get("screen_name", ""),
            })

        media_links = []
        media_types = []
        media_entities = legacy.get("extended_entities", {}).get("media", []) or legacy.get("entities", {}).get("media", [])
        for media_item in media_entities:
            media_type = media_item.get("type", "unknown")
            media_types.append(media_type)
            expanded = media_item.get("expanded_url")
            media_url = media_item.get("media_url_https")
            if expanded:
                media_links.append(expanded)
            elif media_url:
                media_links.append(media_url)

        return {
            "urls": urls,
            "hashtags": hashtags,
            "mentions": mentions,
            "media_links": media_links,
            "media_count": len(media_links),
            "media_types": media_types,
        }

    def _short_tweet_summary(self, tweet: Dict) -> Dict:
        """Small parent/ancestor representation for reply context."""
        return {
            "id": tweet.get("id"),
            "account": tweet.get("account", "unknown"),
            "text": tweet.get("text", ""),
            "timestamp": tweet.get("timestamp", ""),
            "url": tweet.get("url", ""),
            "in_reply_to_status_id": tweet.get("in_reply_to_status_id"),
            "source_language": tweet.get("source_language"),
            "translation_meta": tweet.get("translation_meta"),
        }

    def _attach_conversation_context(self, tweets: List[Dict]) -> None:
        """Attach available ancestor chains to replies from timeline data."""
        by_id = {tweet.get("id"): tweet for tweet in tweets if tweet.get("id")}
        for tweet in tweets:
            if tweet.get("type") != "Reply":
                continue

            parent_id = tweet.get("in_reply_to_status_id")
            chain = []
            seen = {tweet.get("id")}

            while parent_id and parent_id in by_id and parent_id not in seen:
                parent = by_id[parent_id]
                seen.add(parent_id)
                chain.append(self._short_tweet_summary(parent))
                parent_id = parent.get("in_reply_to_status_id")

            chain.reverse()
            if chain:
                tweet["conversation_chain"] = chain
                tweet["parent_tweet"] = chain[-1]

    def _is_account_timeline_item(self, tweet: Dict, account: str) -> bool:
        """Keep account activity as primary records, not every context tweet."""
        return str(tweet.get("account", "")).lower() == account.lower()
    
    def _parse_tweet(self, tweet_obj: Dict) -> Optional[Dict]:
        """Parse tweet object into simplified dict"""
        tweet_obj = self._unwrap_tweet_result(tweet_obj) or tweet_obj
        if not tweet_obj:
            return None
        
        # Handle tombstones
        typename = tweet_obj.get("__typename", "")
        if typename in ["TweetTombstone", "TweetUnavailable"]:
            return None
        
        legacy = tweet_obj.get("legacy", {})
        if not legacy:
            return None
        
        # Basic info
        tweet_id = legacy.get("id_str") or tweet_obj.get("rest_id", "")
        if not tweet_id:
            return None
        
        # User info
        user_result = (
            tweet_obj.get("core", {})
            .get("user_results", {})
            .get("result", {})
        )
        user_core = user_result.get("core", {})
        user_legacy = user_result.get("legacy", {})
        username = user_core.get("screen_name") or user_legacy.get("screen_name", "unknown")
        
        # Timestamp
        created_at = legacy.get("created_at", "")
        timestamp = self._parse_timestamp(created_at)
        
        # Text
        text = self._extract_tweet_text(tweet_obj)
        translation_meta = extract_translation_meta(tweet_obj)
        
        # Metrics (centralized extraction)
        metrics = extract_metrics(tweet_obj)
        likes = self._normalize_metric(metrics.get("likes"))
        retweets = self._normalize_metric(metrics.get("retweets"))
        replies = self._normalize_metric(metrics.get("replies"))
        quotes = self._normalize_metric(metrics.get("quotes"))
        bookmarks = self._normalize_metric(metrics.get("bookmarks"))
        views = self._normalize_metric(metrics.get("views"))
        
        # Determine type
        tweet_type = "Tweet"
        if text.startswith("RT @") or legacy.get("retweeted_status_result"):
            tweet_type = "Retweet"
        elif legacy.get("in_reply_to_status_id_str"):
            tweet_type = "Reply"
        elif legacy.get("quoted_status_id_str") or tweet_obj.get("quoted_status_result"):
            tweet_type = "Quote"

        # Conversation chain metadata
        conversation_id = legacy.get("conversation_id_str")
        in_reply_to_status_id = legacy.get("in_reply_to_status_id_str")
        in_reply_to_user_id = legacy.get("in_reply_to_user_id_str")

        # Retweet / quote source IDs (if present)
        retweeted_tweet_id = None
        quoted_tweet_id = None

        retweeted = legacy.get("retweeted_status_result", {})
        retweeted_author = None
        retweeted_text = ""
        retweeted_timestamp = ""
        retweeted_translation_meta = None
        if retweeted:
            retweeted_result = self._unwrap_tweet_result(retweeted) or {}
            retweeted_legacy = retweeted_result.get("legacy", {})
            retweeted_tweet_id = retweeted_legacy.get("id_str") or retweeted_result.get("rest_id")
            retweeted_user_result = retweeted_result.get("core", {}).get("user_results", {}).get("result", {})
            retweeted_user_core = retweeted_user_result.get("core", {})
            retweeted_user_legacy = retweeted_user_result.get("legacy", {})
            retweeted_author = retweeted_user_core.get("screen_name") or retweeted_user_legacy.get("screen_name")
            retweeted_text = self._extract_tweet_text(retweeted_result)
            retweeted_timestamp = self._parse_timestamp(retweeted_legacy.get("created_at", ""))
            retweeted_translation_meta = extract_translation_meta(retweeted_result)

        quoted = tweet_obj.get("quoted_status_result", {})
        quoted_author = None
        quoted_text = ""
        quoted_timestamp = ""
        quoted_translation_meta = None
        if quoted:
            quoted_result = self._unwrap_tweet_result(quoted) or {}
            quoted_legacy = quoted_result.get("legacy", {})
            quoted_tweet_id = quoted_legacy.get("id_str") or quoted_result.get("rest_id")
            quoted_user_result = quoted_result.get("core", {}).get("user_results", {}).get("result", {})
            quoted_user_core = quoted_user_result.get("core", {})
            quoted_user_legacy = quoted_user_result.get("legacy", {})
            quoted_author = quoted_user_core.get("screen_name") or quoted_user_legacy.get("screen_name")
            quoted_text = self._extract_tweet_text(quoted_result)
            quoted_timestamp = self._parse_timestamp(quoted_legacy.get("created_at", ""))
            quoted_translation_meta = extract_translation_meta(quoted_result)
        elif legacy.get("quoted_status_id_str"):
            quoted_tweet_id = legacy.get("quoted_status_id_str")
        
        # URL
        url = f"https://x.com/{username}/status/{tweet_id}"
        
        entities = self._extract_entities(legacy)

        return {
            "id": tweet_id,
            "type": tweet_type,
            "url": url,
            "timestamp": timestamp,
            "text": text,
            "likes": likes,
            "retweets": retweets,
            "replies": replies,
            "views": views,
            "bookmarks": bookmarks,
            "quotes": quotes,
            "account": username,
            "conversation_id": conversation_id,
            "in_reply_to_status_id": in_reply_to_status_id,
            "in_reply_to_user_id": in_reply_to_user_id,
            "in_reply_to_screen_name": legacy.get("in_reply_to_screen_name"),
            "retweeted_tweet_id": retweeted_tweet_id,
            "retweeted_author": retweeted_author,
            "retweeted_text": retweeted_text,
            "retweeted_timestamp": retweeted_timestamp,
            "quoted_tweet_id": quoted_tweet_id,
            "quoted_author": quoted_author,
            "quoted_text": quoted_text,
            "quoted_timestamp": quoted_timestamp,
            "source_language": translation_meta.get("source_language"),
            "translation_meta": translation_meta,
            "retweeted_translation_meta": retweeted_translation_meta,
            "quoted_translation_meta": quoted_translation_meta,
            "entities": entities,
            "raw_timestamp": created_at,
        }
    
    def get_user_id(self, username: str) -> Optional[str]:
        """Resolve username to user ID"""
        print(f"  🔍 Resolving user ID for @{username}")
        
        query_id = self.api_manager.get_query_id("UserByScreenName")
        url = f"https://x.com/i/api/graphql/{query_id}/UserByScreenName"
        
        variables = {
            "screen_name": username,
            "withSafetyModeUserFields": True
        }
        features = {
            "hidden_profile_subscriptions_enabled": True,
            "rweb_tipjar_consumption_enabled": True,
        }
        
        params = {
            "variables": self._compact_json(variables),
            "features": self._compact_json(features)
        }
        
        response = self.api_manager.make_request(
            "UserByScreenName",
            url,
            params=params,
        )
        if not response:
            self.storage_manager.log_event("fetch_failures", f"Failed to resolve user ID for @{username}")
            return None
        
        try:
            data = response.json()
            user_result = data.get("data", {}).get("user", {}).get("result", {})
            user_id = user_result.get("rest_id")
            
            if user_id:
                print(f"  ✓ User ID: {user_id}")
                return user_id
            else:
                print(f"  ✗ Could not find user ID")
                return None
        except Exception as e:
            print(f"  ✗ Error parsing response: {e}")
            return None
    
    def fetch_user_tweets(self, user_id: str, max_pages: int = 20, username: Optional[str] = None) -> List[Dict]:
        """Fetch tweets from UserTweets endpoint"""
        print(f"  📄 Fetching tweets (UserTweets endpoint)...")
        
        query_id = self.api_manager.get_query_id("UserTweets")
        url = f"https://x.com/i/api/graphql/{query_id}/UserTweets"
        
        all_tweets = []
        seen_ids = set()
        cursor = None
        page = 1
        progress_state = self._build_timeline_progress_state()
        
        while page <= max_pages:
            if cursor and not self._can_use_cursor(progress_state, cursor):
                self.storage_manager.log_event(
                    "cursor_blacklist",
                    f"UserTweets @{username or user_id}: cursor blacklisted after repeated attempts; cursor={cursor}",
                )
                self.storage_manager.log_event(
                    "pagination_terminated",
                    f"UserTweets @{username or user_id}: pagination terminated cleanly (cursor attempt budget exhausted)",
                )
                break

            variables = {
                "userId": user_id,
                "count": 20,
                "includePromotedContent": True,
                "withQuickPromoteEligibilityTweetFields": True,
                "withVoice": True,
            }
            
            if cursor:
                variables["cursor"] = cursor
            
            features = self._timeline_features()
            
            params = {
                "variables": self._compact_json(variables),
                "features": self._compact_json(features),
                "fieldToggles": self._compact_json({"withArticlePlainText": False}),
            }
            
            response = self.api_manager.make_request(
                "UserTweets",
                url,
                params=params,
                headers={
                    "referer": f"https://x.com/i/user/{user_id}",
                    "x-twitter-active-user": "yes",
                },
            )
            if not response:
                if self._should_try_single_reseed(progress_state, cursor):
                    self.storage_manager.log_event(
                        "cursor_recovery",
                        f"UserTweets @{username or user_id}: request failed on cursor; single reseed allowed",
                    )
                    cursor = None
                    progress_state["no_progress_pages"] += 1
                    self._random_human_pause("replies_retry")
                    continue
                self.storage_manager.log_event(
                    "pagination_terminated",
                    f"UserTweets @{username or user_id}: pagination terminated cleanly (request failed without recoverable progression)",
                )
                break
            
            try:
                data = response.json()
                instructions = (
                    data.get("data", {})
                    .get("user", {})
                    .get("result", {})
                    .get("timeline", {})
                    .get("timeline", {})
                    .get("instructions", [])
                )

                page_result = self._parse_timeline_page(
                    instructions=instructions,
                    seen_ids=seen_ids,
                    include_conversation_modules=False,
                )
                page_tweets = page_result["tweets"]
                all_tweets.extend(page_tweets)
                next_cursor = page_result["next_cursor"]
                new_items_on_page = len(page_tweets)
                reached_time_limit = bool(page_result["reached_time_limit"])

                stall_reason = self._classify_stall_reason(
                    cursor=cursor,
                    next_cursor=next_cursor,
                    has_entries=bool(page_result["has_entries"]),
                    timeline_item_count=int(page_result["timeline_item_count"]),
                    timeline_module_count=0,
                    new_items_count=new_items_on_page,
                )
                if (
                    cursor
                    and next_cursor
                    and next_cursor in progress_state["seen_cursor_values"]
                ):
                    stall_reason = "repeated_cursor_history"

                if new_items_on_page > 0:
                    progress_state["had_progress_before_stall"] = True
                    progress_state["last_new_tweet_total"] = len(all_tweets)
                    progress_state["no_progress_pages"] = 0
                else:
                    progress_state["no_progress_pages"] += 1

                if stall_reason:
                    if "repeated_cursor" in stall_reason:
                        self.storage_manager.log_event(
                            "repeated_cursor_detected",
                            f"UserTweets @{username or user_id}: {stall_reason} | cursor={cursor or 'none'} | next={next_cursor or 'none'}",
                        )
                    if "no_new_tweets" in stall_reason:
                        self.storage_manager.log_event(
                            "no_new_tweets_detected",
                            f"UserTweets @{username or user_id}: no new tweets on cursor page | cursor={cursor or 'none'}",
                        )
                    if stall_reason in {"empty_entries", "no_timeline_items_or_modules", "no_new_tweets_detected"}:
                        self.storage_manager.log_event(
                            "no_progression_detected",
                            f"UserTweets @{username or user_id}: {stall_reason} | cursor={cursor or 'none'}",
                        )
                    self.storage_manager.log_event(
                        "cursor_exhausted",
                        f"UserTweets @{username or user_id}: {stall_reason} | cursor={cursor or 'none'} | next={next_cursor or 'none'}",
                    )
                    if self._should_try_single_reseed(progress_state, cursor):
                        self.storage_manager.log_event(
                            "cursor_recovery",
                            f"UserTweets @{username or user_id}: single reseed after {stall_reason}",
                        )
                        cursor = None
                        self._random_human_pause("replies_retry")
                        continue
                    self.storage_manager.log_event(
                        "pagination_terminated",
                        f"UserTweets @{username or user_id}: pagination terminated cleanly ({stall_reason})",
                    )
                    break

                if reached_time_limit:
                    self.storage_manager.log_event(
                        "pagination_terminated",
                        f"UserTweets @{username or user_id}: reached history window and terminated cleanly",
                    )
                    break

                if not next_cursor:
                    self.storage_manager.log_event(
                        "pagination_terminated",
                        f"UserTweets @{username or user_id}: no further bottom cursor; terminated cleanly",
                    )
                    break

                progress_state["seen_cursor_values"].add(str(next_cursor))
                progress_state["cursor_generation_history"].append(str(next_cursor))
                self.storage_manager.log_event(
                    "cursor_accepted",
                    f"UserTweets @{username or user_id}: cursor accepted {str(next_cursor)[:36]}...",
                )
                cursor = next_cursor
                page += 1
                print(f"    Page {page}: {len(all_tweets)} items so far")
                self._random_human_pause("between_pages")
                
            except Exception as e:
                print(f"  ✗ Error parsing page {page}: {e}")
                self.storage_manager.log_event("fetch_failures", f"UserTweets page {page} error: {e}")
                break
        
        print(f"  ✓ UserTweets: {len(all_tweets)} items")
        return all_tweets
    
    def fetch_user_tweets_and_replies(self, user_id: str, username: str, max_pages: int = 20) -> List[Dict]:
        """Fetch tweets from UserTweetsAndReplies endpoint"""
        print(f"  📄 Fetching replies (UserTweetsAndReplies endpoint)...")
        
        query_id = self.api_manager.get_query_id("UserTweetsAndReplies")
        if not query_id:
            self.storage_manager.log_event("fetch_failures", "Missing query ID for UserTweetsAndReplies in config")
            print("  ✗ Missing UserTweetsAndReplies query ID in config")
            return []
        url = f"https://x.com/i/api/graphql/{query_id}/UserTweetsAndReplies"
        
        all_tweets = []
        cursor = None
        page = 1
        contexts = self.api_manager.get_context_variants("UserTweetsAndReplies", username)
        seen_ids = set()
        progress_state = self._build_timeline_progress_state()
        last_success_tweet_id: Optional[str] = None
        
        while page <= max_pages:
            if cursor and not self._can_use_cursor(progress_state, cursor):
                self.storage_manager.log_event(
                    "cursor_blacklist",
                    f"UserTweetsAndReplies @{username}: cursor blacklisted after repeated attempts; cursor={cursor}",
                )
                self.storage_manager.log_event(
                    "pagination_terminated",
                    f"UserTweetsAndReplies @{username}: pagination terminated cleanly (cursor attempt budget exhausted)",
                )
                break

            variables = {
                "userId": user_id,
                "count": 20,
                "includePromotedContent": True,
                "withCommunity": True,
                "withVoice": True,
            }
            
            if cursor:
                variables["cursor"] = cursor
            
            features = self._timeline_features()
            
            params = {
                "variables": self._compact_json(variables),
                "features": self._compact_json(features),
                "fieldToggles": self._compact_json({"withArticlePlainText": False}),
            }

            if cursor is None:
                self.api_manager.warmup_navigation_context(
                    username=username,
                    endpoint="UserTweetsAndReplies",
                )
                warmup_seconds = int(self.config.get("api_config", {}).get("replies_warmup_seconds", 3))
                if warmup_seconds > 0:
                    time.sleep(warmup_seconds)
            
            response = None
            retries = int(self.config.get("api_config", {}).get("replies_max_retries", 3))
            for attempt in range(retries):
                for ctx in contexts:
                    response = self.api_manager.make_request(
                        "UserTweetsAndReplies",
                        url,
                        params=params,
                        context=ctx,
                        max_retries=1,
                    )
                    if response:
                        break
                if response:
                    break
                if attempt < retries - 1:
                    self._random_human_pause("replies_retry")

            if not response:
                if cursor and self.api_manager.get_last_status("UserTweetsAndReplies") == 404:
                    self.storage_manager.log_event(
                        "cursor_exhausted",
                        f"UserTweetsAndReplies @{username}: cursor 404 detected | cursor={cursor} | last_success_tweet_id={last_success_tweet_id or 'unknown'}",
                    )
                    if self._should_try_single_reseed(progress_state, cursor):
                        self.storage_manager.log_event(
                            "cursor_recovery",
                            f"UserTweetsAndReplies @{username}: single reseed after cursor 404",
                        )
                        cursor = None
                        self._random_human_pause("replies_retry")
                        continue
                    self.storage_manager.log_event(
                        "pagination_terminated",
                        f"UserTweetsAndReplies @{username}: terminated cleanly after cursor 404 exhaustion",
                    )
                    break
                # Log but don't fail - graceful degradation
                health = self.api_manager.get_endpoint_health("UserTweetsAndReplies")
                self.storage_manager.log_event("endpoint_health", f"UserTweetsAndReplies for @{username}: {health}")
                break
            
            try:
                data = response.json()
                instructions = (
                    data.get("data", {})
                    .get("user", {})
                    .get("result", {})
                    .get("timeline", {})
                    .get("timeline", {})
                    .get("instructions", [])
                )

                page_result = self._parse_timeline_page(
                    instructions=instructions,
                    seen_ids=seen_ids,
                    include_conversation_modules=True,
                )
                page_tweets = page_result["tweets"]
                all_tweets.extend(page_tweets)
                if page_tweets:
                    last_success_tweet_id = page_tweets[-1]["id"]

                next_cursor = page_result["next_cursor"]
                new_items_on_page = len(page_tweets)
                reached_time_limit = bool(page_result["reached_time_limit"])

                stall_reason = self._classify_stall_reason(
                    cursor=cursor,
                    next_cursor=next_cursor,
                    has_entries=bool(page_result["has_entries"]),
                    timeline_item_count=int(page_result["timeline_item_count"]),
                    timeline_module_count=int(page_result["timeline_module_count"]),
                    new_items_count=new_items_on_page,
                )
                if (
                    cursor
                    and next_cursor
                    and next_cursor in progress_state["seen_cursor_values"]
                ):
                    stall_reason = "repeated_cursor_history"

                if new_items_on_page > 0:
                    progress_state["had_progress_before_stall"] = True
                    progress_state["last_new_tweet_total"] = len(all_tweets)
                    progress_state["no_progress_pages"] = 0
                else:
                    progress_state["no_progress_pages"] += 1

                if stall_reason:
                    if "repeated_cursor" in stall_reason:
                        self.storage_manager.log_event(
                            "repeated_cursor_detected",
                            f"UserTweetsAndReplies @{username}: {stall_reason} | cursor={cursor or 'none'} | next={next_cursor or 'none'}",
                        )
                    if "no_new_tweets" in stall_reason:
                        self.storage_manager.log_event(
                            "no_new_tweets_detected",
                            f"UserTweetsAndReplies @{username}: no new tweets on cursor page | cursor={cursor or 'none'}",
                        )
                    if stall_reason in {"empty_entries", "no_timeline_items_or_modules", "no_new_tweets_detected"}:
                        self.storage_manager.log_event(
                            "no_progression_detected",
                            f"UserTweetsAndReplies @{username}: {stall_reason} | cursor={cursor or 'none'}",
                        )
                    self.storage_manager.log_event(
                        "cursor_exhausted",
                        f"UserTweetsAndReplies @{username}: {stall_reason} | cursor={cursor or 'none'} | next={next_cursor or 'none'}",
                    )
                    if self._should_try_single_reseed(progress_state, cursor):
                        self.storage_manager.log_event(
                            "cursor_recovery",
                            f"UserTweetsAndReplies @{username}: single reseed after {stall_reason}",
                        )
                        cursor = None
                        self._random_human_pause("replies_retry")
                        continue
                    self.storage_manager.log_event(
                        "pagination_terminated",
                        f"UserTweetsAndReplies @{username}: pagination terminated cleanly ({stall_reason})",
                    )
                    break

                if reached_time_limit:
                    self.storage_manager.log_event(
                        "pagination_terminated",
                        f"UserTweetsAndReplies @{username}: reached history window and terminated cleanly",
                    )
                    break

                if not next_cursor:
                    self.storage_manager.log_event(
                        "pagination_terminated",
                        f"UserTweetsAndReplies @{username}: no further bottom cursor; terminated cleanly",
                    )
                    break

                progress_state["seen_cursor_values"].add(str(next_cursor))
                progress_state["cursor_generation_history"].append(str(next_cursor))
                self.storage_manager.log_event(
                    "cursor_accepted",
                    f"UserTweetsAndReplies @{username}: cursor accepted {str(next_cursor)[:36]}...",
                )
                cursor = next_cursor
                page += 1
                print(f"    Page {page}: {len(all_tweets)} items so far")
                self._random_human_pause("between_pages")
                
            except Exception as e:
                print(f"  ✗ Error parsing page {page}: {e}")
                self.storage_manager.log_event("fetch_failures", f"UserTweetsAndReplies page {page} error: {e}")
                break
        
        print(f"  ✓ UserTweetsAndReplies: {len(all_tweets)} items")
        return all_tweets
    
    def fetch_account(self, username: str) -> bool:
        """
        Fetch account data and distribute to 4 folders
        
        This is the key method that implements the new architecture:
        1. Fetch both endpoints once
        2. Compare endpoints
        3. Distribute to 4 folders
        4. Register in dedupe registry
        """
        print(f"  📥 Fetching data for @{username}...")
        policy = get_priority_policy(username, self.account_map, self.priority_policies)
        self.active_history_days = int(policy.get("historical_window_days", HISTORY_DAYS))
        
        # Get user ID
        user_id = self.get_user_id(username)
        if not user_id:
            return False
        
        # Determine page limit based on tier policy.
        max_pages = int(policy.get("historical_max_pages", DEFAULT_HISTORICAL_MAX_PAGES))
        print(
            f"  🧭 Policy: P{policy['priority']} | "
            f"history={self.active_history_days}d | max_pages={max_pages}"
        )
        
        # Fetch both endpoints
        tweets_only = self.fetch_user_tweets(user_id, max_pages, username=username)
        
        replies_endpoint_ok = True
        tweets_and_replies = []
        if FETCH_REPLIES:
            tweets_and_replies = self.fetch_user_tweets_and_replies(user_id, username, max_pages)
            replies_health = self.api_manager.get_endpoint_health("UserTweetsAndReplies")
            if not tweets_and_replies and replies_health in [
                "stale_query_id",
                "context_rejected",
                "rate_limited",
                "server_error",
                "unknown_error",
            ]:
                replies_endpoint_ok = False
        else:
            print(f"  ⏭️  Skipping replies (FETCH_REPLIES=False)")
        
        # Only mirror UserTweets when replies endpoint is healthy but empty.
        if replies_endpoint_ok and not tweets_and_replies:
            tweets_and_replies = tweets_only.copy()
        
        # Compare endpoints (fetch once, distribute many)
        print(f"  🔄 Comparing endpoints...")
        only, replies, merged, diffs = self.storage_manager.compare_endpoints(
            tweets_only, tweets_and_replies
        )
        
        print(f"  ✓ Comparison complete:")
        print(f"    - Tweets only: {len(only)} items")
        print(f"    - Tweets and replies: {len(replies)} items")
        print(f"    - Merged: {len(merged)} items")
        print(f"    - Diffs: {len(diffs)} items")

        # Attach reply chain context from already-fetched timelines (no TweetDetail call needed)
        self._attach_conversation_context(only)
        self._attach_conversation_context(replies)
        self._attach_conversation_context(merged)
        self._attach_conversation_context(diffs)

        only = [tweet for tweet in only if self._is_account_timeline_item(tweet, username)]
        replies = [tweet for tweet in replies if self._is_account_timeline_item(tweet, username)]
        merged = [tweet for tweet in merged if self._is_account_timeline_item(tweet, username)]
        diffs = [tweet for tweet in diffs if self._is_account_timeline_item(tweet, username)]
        
        # Register all unique tweets in dedupe registry
        all_unique_ids = set()
        for tweet in merged:
            all_unique_ids.add(tweet["id"])
        
        for tweet_id in all_unique_ids:
            if not self.storage_manager.is_tweet_seen(tweet_id):
                stored_locations = ["USER_TWEETS"]
                if replies_endpoint_ok:
                    stored_locations.extend(["USER_TWEETS_AND_REPLIES", "MERGED_TIMELINES"])
                self.storage_manager.register_tweet(tweet_id, username, stored_locations)
        
        # Save to 4 folders (grouped by date)
        print(f"  💾 Saving to files...")
        
        # Group tweets by date
        tweets_by_date = {}
        save_sets = [(only, "USER_TWEETS")]
        if replies_endpoint_ok:
            save_sets.extend([
                (replies, "USER_TWEETS_AND_REPLIES"),
                (merged, "MERGED_TIMELINES"),
                (diffs, "ENDPOINT_DIFFS"),
            ])
        else:
            print("  ⚠️  UserTweetsAndReplies unavailable; skipping replies/merged/diffs output for this account")

        for tweet_list, folder_name in save_sets:
            for tweet in tweet_list:
                # Extract date from timestamp
                try:
                    # Timestamp format: "1405-02-23 12:34:56"
                    date_str = tweet["timestamp"].split()[0]
                except:
                    date_str = self.storage_manager.get_jalali_date()
                
                key = (folder_name, date_str)
                if key not in tweets_by_date:
                    tweets_by_date[key] = []
                tweets_by_date[key].append(tweet)
        
        # Write files
        saved_count = 0
        for (folder_name, date_str), tweets in tweets_by_date.items():
            folder_map = {
                "USER_TWEETS": self.storage_manager.user_tweets_dir,
                "USER_TWEETS_AND_REPLIES": self.storage_manager.user_replies_dir,
                "MERGED_TIMELINES": self.storage_manager.merged_dir,
                "ENDPOINT_DIFFS": self.storage_manager.endpoint_diffs_dir,
            }
            
            folder = folder_map[folder_name]
            count = self.storage_manager.save_tweets_to_file(
                tweets, username, date_str, folder
            )
            
            if count > 0:
                print(f"    ✓ Saved {count} items to {folder_name}/{username.upper()}/{date_str}.txt")
                saved_count += count
        
        print(f"  ✅ Complete - {saved_count} total items saved")
        return True


# ============================================================================
# MAIN
# ============================================================================

def main():
    print(SEP)
    print("Twitter Historical Tweet Fetcher - Hybrid")
    print(SEP)
    print("\nUsing new architecture:")
    print("  - api_manager.py for networking")
    print("  - storage_manager.py for storage")
    print("  - Endpoint comparison (4 folders)")
    print("  - Global dedupe registry")
    print("  - Graceful degradation\n")
    
    fetcher = TwitterHistoricalFetcher()
    accounts_to_run = ACCOUNTS or ordered_accounts(fetcher.account_map)
    if not accounts_to_run:
        print("\n⚠️  No accounts configured in ACCOUNTS or tier_configuration.")
        sys.exit(1)
    
    print(f"\n📋 Processing {len(accounts_to_run)} account(s): {', '.join(accounts_to_run)}\n")
    
    successful, failed = [], []
    
    for i, account in enumerate(accounts_to_run, 1):
        print(f"\n[{i}/{len(accounts_to_run)}] @{account}")
        print("-" * 70)
        
        try:
            ok = fetcher.fetch_account(account)
            (successful if ok else failed).append(account)
        except Exception as e:
            print(f"  ✗ Fatal error for @{account}: {e}")
            fetcher.storage_manager.log_event("fetch_failures", f"Fatal error for @{account}: {e}")
            failed.append(account)
        
        if i < len(accounts_to_run):
            sim = fetcher.config.get("anti_bot_simulation", {})
            delay_cfg = sim.get("delays_seconds", {})
            min_d = float(delay_cfg.get("between_accounts_min", 3))
            max_d = float(delay_cfg.get("between_accounts_max", 6))
            if max_d < min_d:
                max_d = min_d
            delay = random.uniform(min_d, max_d)
            print(f"\n  ⏸️  Pausing {delay:.1f}s before next account...")
            time.sleep(delay)
    
    # Summary
    print("\n" + SEP)
    print("SUMMARY")
    print(SEP)
    
    print(f"\n✅ Successful: {len(successful)}/{len(accounts_to_run)}")
    for a in successful:
        print(f"   ✓ @{a}")
    
    if failed:
        print(f"\n⚠️  Failed: {len(failed)}/{len(accounts_to_run)}")
        for a in failed:
            print(f"   ✗ @{a}")
    
    # Display statistics
    print(f"\n📊 Session Statistics:")
    stats = fetcher.api_manager.get_stats()
    print(f"   - Requests made: {stats['requests_made']}")
    print(f"   - Session duration: {stats['session_duration_seconds']}s")
    print(f"   - Requests per minute: {stats['requests_per_minute']}")
    
    print(f"\n📁 Output Folders:")
    print(f"   - data/USER_TWEETS/")
    print(f"   - data/USER_TWEETS_AND_REPLIES/")
    print(f"   - data/MERGED_TIMELINES/")
    print(f"   - data/ENDPOINT_DIFFS/")
    print(f"   - data/STATE/ (dedupe registry)")
    print(f"   - logs/ (structured logs)")
    
    print("\n✅ All done!")
    print(SEP)


if __name__ == "__main__":
    main()
