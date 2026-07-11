#!/usr/bin/env python3
"""
Twitter Historical Tweet Fetcher - Unified Timeline

Fetches tweets, retweets, and replies into unified daily files.

CONFIGURATION:
  1. Run setup_api_cookies.py first to configure cookies
  2. Edit config.json to update query IDs if you get 404 errors
  3. See CONFIG_GUIDE.md for detailed instructions

TROUBLESHOOTING:
  - 401 Unauthorized → Update cookies in config.json
  - 404 Not Found → Update query IDs in config.json (see CONFIG_GUIDE.md)
  - Rate limited → Wait 15 minutes, reduce number of accounts

For help: Read CONFIG_GUIDE.md
"""

import random
import re

# ============================================================================
# CONFIGURATION — edit only this section
# ============================================================================

ACCOUNTS = ["elonmusk","paulg"]

# Days of history to fetch
TWO_WEEK_DAYS = 3

# Skip replies endpoint (currently returns 404)
FETCH_REPLIES = True

# Output root folder (created next to this script)
OUTPUT_FOLDER = "TWEETS"

# Tehran timezone + Jalali calendar
TIMEZONE = "Asia/Tehran"

# Random pause between paginated requests (seconds)
MIN_DELAY = 2
MAX_DELAY = 5

# ============================================================================
# DO NOT EDIT BELOW THIS LINE
# ============================================================================

import json
import os
import subprocess
import sys
import time
import textwrap
import uuid
import base64
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import requests
    import jdatetime
    import pytz
except ImportError:
    print("ERROR: Missing dependencies. Run: pip3 install requests jdatetime pytz")
    sys.exit(1)

SEP = "═" * 70


class TwitterUnifiedFetcher:
    """Fetches tweets, retweets, and replies into unified daily files."""

    def __init__(self, config_path: str = "config.json"):
        self.base_dir = Path(__file__).parent
        self.config = self._load_config(config_path)
        if self._config_needs_setup(self.config):
            setup_script = self.base_dir / "setup_api_cookies.py"
            print("\n⚠️  Config is incomplete. Running setup_api_cookies.py...\n")
            subprocess.run([sys.executable, str(setup_script)], check=False)
            self.config = self._load_config(config_path)
        
        # Load query IDs from config (with fallback defaults)
        api_config = self.config.get('api_config', {})
        self.USER_BY_SCREEN_NAME_QUERY_ID = api_config.get('user_by_screen_name_query_id', 'sLVLhk0bGj3MVFEKTdax1w')
        self.USER_TWEETS_QUERY_ID = api_config.get('user_tweets_query_id', 'naBcZ4al-iTCFBYGOAMzBQ')
        self.USER_TWEETS_AND_REPLIES_QUERY_ID = api_config.get('user_tweets_and_replies_query_id', '6eh3huj6fJnA3Naupj4w0Q')
        self.TWEET_DETAIL_QUERY_ID = api_config.get('tweet_detail_query_id', '')
        self.REPLIES_WARMUP_SECONDS = api_config.get('replies_warmup_seconds', 3)
        self.REPLIES_MAX_RETRIES = api_config.get('replies_max_retries', 3)
        
        self.session = requests.Session()
        self.tz = pytz.timezone(TIMEZONE)
        self.tweets_root = self.base_dir / OUTPUT_FOLDER
        self.tweets_root.mkdir(exist_ok=True)
        self.rate_limit_state = {}  # Track rate limits per endpoint
        self._setup_session()

    def _load_config(self, config_path: str) -> dict:
        full_path = self.base_dir / config_path
        with open(full_path, "r") as f:
            return json.load(f)

    def _config_needs_setup(self, config: Dict) -> bool:
        cookies = config.get("api_cookies", {})
        api_auth = config.get("api_auth", {})
        api_config = config.get("api_config", {})
        return not all([
            cookies.get("auth_token"),
            cookies.get("ct0"),
            api_auth.get("bearer_token"),
            api_config.get("user_by_screen_name_query_id"),
            api_config.get("user_tweets_query_id"),
            api_config.get("user_tweets_and_replies_query_id"),
        ])

    def _check_cookies_valid(self) -> bool:
        cookies = self.config.get("api_cookies", {})
        return all(cookies.get(k) for k in ("auth_token", "ct0"))

    def _setup_session(self):
        if not self._check_cookies_valid():
            print("\n⚠️  Cookies missing/invalid — run: python3 setup_api_cookies.py\n")
            sys.exit(1)

        cookies = self.config.get("api_cookies", {})
        for k, v in cookies.items():
            self.session.cookies.set(k, v, domain=".x.com")

        bearer = self.config.get("api_auth", {}).get("bearer_token", "")
        csrf = cookies.get("ct0", "")
        configured_headers = self.config.get("api_headers", {})
        
        # Generate dynamic transaction ID per session
        tx_id = self._generate_transaction_id()

        self.session.headers.update({
            "authorization": f"Bearer {bearer}",
            "x-csrf-token": csrf,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "x-client-transaction-id": tx_id,
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            "referer": "https://x.com/",
            "accept": "*/*",
            "content-type": "application/json",
            "dnt": "1",
            "priority": "u=1, i",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        })
        if configured_headers:
            self.session.headers.update({k: str(v) for k, v in configured_headers.items() if v})
        print("✓ Session configured with cookies")
        print("\n🔍 DEBUG INFO:")
        print(f"  - Cookies loaded: {len(self.session.cookies)} cookies")
        for cookie in self.session.cookies:
            print(f"    • {cookie.name}: {cookie.value[:20]}..." if len(cookie.value) > 20 else f"    • {cookie.name}: {cookie.value}")
        print(f"  - Bearer token: {bearer[:50]}...")
        print(f"  - CSRF token (ct0): {csrf[:20]}...")

    def _generate_transaction_id(self) -> str:
        """Generate a fresh transaction ID per session."""
        raw = uuid.uuid4().bytes + int(time.time() * 1000).to_bytes(8, 'big')
        tx_id = base64.urlsafe_b64encode(raw).decode()[:72]
        return tx_id

    def _check_rate_limit(self, response: requests.Response, endpoint: str):
        """Check rate limit headers and sleep if necessary."""
        try:
            remaining = int(response.headers.get("x-rate-limit-remaining", 1))
            reset_time = int(response.headers.get("x-rate-limit-reset", 0))
            
            # Store state
            self.rate_limit_state[endpoint] = {
                "remaining": remaining,
                "reset": reset_time
            }
            
            # If we're at or near the limit, sleep until reset
            if remaining == 0 and reset_time > 0:
                sleep_duration = max(reset_time - time.time(), 0) + 5
                print(f"  ⏳ Rate limit hit for {endpoint}, sleeping {sleep_duration:.0f}s...")
                time.sleep(sleep_duration)
            elif remaining < 5:  # Warning threshold
                print(f"  ⚠️  Rate limit low for {endpoint}: {remaining} remaining")
        except (ValueError, TypeError):
            pass

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """Make HTTP request with 5xx retry logic."""
        api_config = self.config.get("api_config", {})
        max_attempts = api_config.get("retry_5xx_attempts", 3)
        base_delay = api_config.get("retry_5xx_base_delay", 2)
        
        last_error = None
        for attempt in range(max_attempts):
            try:
                response = self.session.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response else 0
                
                # Retry on 5xx errors
                if 500 <= status_code < 600:
                    if attempt < max_attempts - 1:
                        jitter = random.uniform(0, 1)
                        sleep_time = (base_delay ** (attempt + 1)) + jitter
                        print(f"  ⚠️  {status_code} error, retrying in {sleep_time:.1f}s (attempt {attempt + 1}/{max_attempts})")
                        time.sleep(sleep_time)
                        last_error = e
                        continue
                
                # Don't retry on 4xx or other errors
                raise
            except Exception as e:
                raise
        
        raise last_error if last_error else Exception("Max retries exceeded")

    def _compact_json(self, payload: Dict) -> str:
        return json.dumps(payload, separators=(",", ":"))

    def _request_with_context(self, url: str, params: Dict, referer: str, active_user: str) -> Dict:
        headers = dict(self.session.headers)
        headers["referer"] = referer
        headers["x-twitter-active-user"] = active_user
        
        # Use retry logic for 5xx errors
        r = self._request_with_retry("GET", url, params=params, headers=headers, timeout=60)
        
        # Check rate limits
        endpoint = url.split("/")[-1].split("?")[0]
        self._check_rate_limit(r, endpoint)
        
        return r.json()

    def _http_status(self, error: Exception) -> Optional[int]:
        response = getattr(error, "response", None)
        return getattr(response, "status_code", None)

    def _warmup_replies_page(self, username: str):
        """Warm up /with_replies context before GraphQL replies calls."""
        if not username:
            return
        warmup_url = f"https://x.com/{username}/with_replies"
        try:
            self.session.get(warmup_url, timeout=30)
            if self.REPLIES_WARMUP_SECONDS > 0:
                time.sleep(self.REPLIES_WARMUP_SECONDS)
        except Exception:
            # Warmup is best-effort; continue even if it fails.
            pass

    # ------------------------------------------------------------------
    # Date helpers
    # ------------------------------------------------------------------

    def _to_jalali_date(self, ts: str) -> str:
        try:
            dt = datetime.strptime(ts, "%a %b %d %H:%M:%S %z %Y").astimezone(self.tz)
            return jdatetime.datetime.fromgregorian(datetime=dt).strftime("%Y-%m-%d")
        except Exception:
            return "unknown"

    def _to_jalali_datetime(self, ts: str) -> str:
        try:
            dt = datetime.strptime(ts, "%a %b %d %H:%M:%S %z %Y").astimezone(self.tz)
            jd = jdatetime.datetime.fromgregorian(datetime=dt)
            return f"{jd.strftime('%Y/%m/%d')} - {jd.strftime('%H:%M')}"
        except Exception:
            return ts

    def _is_within_timeframe(self, ts: str) -> bool:
        try:
            dt = datetime.strptime(ts, "%a %b %d %H:%M:%S %z %Y")
            cutoff = datetime.now(dt.tzinfo) - timedelta(days=TWO_WEEK_DAYS)
            return dt >= cutoff
        except Exception:
            return True

    def _parse_timestamp(self, ts: str) -> Optional[datetime]:
        try:
            return datetime.strptime(ts, "%a %b %d %H:%M:%S %z %Y")
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _wrap(text: str, width: int = 70) -> str:
        lines = []
        for para in text.split("\n"):
            if para:
                lines.append(
                    textwrap.fill(
                        para, width=width, break_long_words=False, break_on_hyphens=False
                    )
                )
            else:
                lines.append("")
        return "\n".join(lines)

    def _expand_urls_in_text(self, text: str, entities: Dict) -> str:
        """Replace t.co URLs in text with their expanded versions."""
        urls = entities.get("urls", [])
        for url_obj in urls:
            short_url = url_obj.get("short", "")
            expanded_url = url_obj.get("expanded", "")
            if short_url and expanded_url and short_url in text:
                text = text.replace(short_url, expanded_url)
        return text

    def _tweet_full_text(self, tweet_obj: Dict) -> str:
        """Prefer Note Tweet text so long posts are not cut at legacy.full_text."""
        note_result = (
            tweet_obj.get("note_tweet", {})
            .get("note_tweet_results", {})
            .get("result", {})
        )
        note_text = note_result.get("text")
        if note_text:
            return note_text
        return tweet_obj.get("legacy", {}).get("full_text", "")

    def _unwrap_tweet_result(self, wrapper: Dict) -> Optional[Dict]:
        """Handle X API wrappers that may omit a direct result key."""
        if not isinstance(wrapper, dict):
            return None
        
        # Check for tombstone at wrapper level
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

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    def get_user_id(self, username: str) -> Optional[str]:
        url = f"https://x.com/i/api/graphql/{self.USER_BY_SCREEN_NAME_QUERY_ID}/UserByScreenName"
        params = {
            "variables": json.dumps(
                {"screen_name": username, "withSafetyModeUserFields": True}
            ),
            "features": json.dumps(
                {
                    "hidden_profile_subscriptions_enabled": True,
                    "rweb_tipjar_consumption_enabled": True,
                }
            ),
        }
        
        print(f"\n  🔍 DEBUG: Resolving user ID for @{username}")
        print(f"     URL: {url}")
        print(f"     Headers: {dict(self.session.headers)}")
        
        # Retry logic for network errors
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    wait_time = 5 * attempt
                    print(f"     Retry {attempt}/{max_retries-1} after {wait_time}s...")
                    time.sleep(wait_time)
                
                print(f"     Making request...")
                r = self.session.get(url, params=params, timeout=60)
                print(f"     Response status: {r.status_code}")
                print(f"     Response headers: {dict(r.headers)}")
            
                if r.status_code != 200:
                    print(f"     Response body: {r.text[:500]}")
            
                r.raise_for_status()
                data = r.json()
                print(f"     Response keys: {list(data.keys())}")
            
                user_id = data["data"]["user"]["result"]["rest_id"]
                print(f"  ✓ User ID: {user_id}")
                return user_id
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries - 1:
                    print(f"  ⚠️  Network error: {type(e).__name__} - {e}")
                    continue
                else:
                    print(f"  ✗ Failed after {max_retries} attempts")
                    print(f"  ✗ Error type: {type(e).__name__}")
                    print(f"  ✗ Error details: {e}")
                    return None
            except Exception as e:
                print(f"  ✗ Error type: {type(e).__name__}")
                print(f"  ✗ Error details: {e}")
                import traceback
                print(f"  ✗ Traceback:")
                traceback.print_exc()
                return None

    def get_user_tweets(
        self, user_id: str, cursor: Optional[str] = None
    ) -> Dict:
        url = f"https://x.com/i/api/graphql/{self.USER_TWEETS_QUERY_ID}/UserTweets"
        variables = {
            "userId": user_id,
            "count": 20,
            "includePromotedContent": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
        }
        if cursor:
            variables["cursor"] = cursor

        params = {
            "variables": self._compact_json(variables),
            "features": self._compact_json(
                {
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
            ),
            "fieldToggles": self._compact_json({"withArticlePlainText": False}),
        }
        try:
            return self._request_with_context(
                url=url,
                params=params,
                referer=f"https://x.com/i/user/{user_id}",
                active_user="yes",
            )
        except Exception as e:
            print(f"  ✗ API request failed: {e}")
            return {}

    def get_user_tweets_and_replies(
        self, user_id: str, cursor: Optional[str] = None, username: Optional[str] = None
    ) -> Dict:
        url = f"https://x.com/i/api/graphql/{self.USER_TWEETS_AND_REPLIES_QUERY_ID}/UserTweetsAndReplies"
        variables = {
            "userId": user_id,
            "count": 20,
            "includePromotedContent": True,
            "withCommunity": True,
            "withVoice": True,
        }
        if cursor:
            variables["cursor"] = cursor

        params = {
            "variables": self._compact_json(variables),
            "features": self._compact_json(
                {
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
            ),
            "fieldToggles": self._compact_json({"withArticlePlainText": False}),
        }
        # Warm up only on first page request.
        if cursor is None and username:
            self._warmup_replies_page(username)

        # Try multiple header contexts because this endpoint is sensitive.
        contexts = [
            {"referer": f"https://x.com/{username}/with_replies" if username else "https://x.com/", "active_user": "no"},
            {"referer": f"https://x.com/{username}/with_replies" if username else "https://x.com/", "active_user": "yes"},
            {"referer": "https://x.com/", "active_user": "yes"},
        ]

        last_error = None
        for attempt in range(self.REPLIES_MAX_RETRIES):
            for ctx in contexts:
                try:
                    return self._request_with_context(
                        url=url,
                        params=params,
                        referer=ctx["referer"],
                        active_user=ctx["active_user"],
                    )
                except Exception as e:
                    if cursor and self._http_status(e) == 404:
                        print("  ✓ Replies pagination ended (X returned a stale cursor)")
                        return {}
                    last_error = e
            # Small backoff between rounds
            time.sleep(1 + attempt)

        if cursor and self._http_status(last_error) == 404:
            print("  ✓ Replies pagination ended (X returned a stale cursor)")
            return {}

        print(f"  ✗ API request failed: {last_error}")
        return {}

    def get_tweet_detail(self, tweet_id: str) -> Dict:
        """Fetch one tweet thread/detail payload for a specific tweet ID."""
        if not self.TWEET_DETAIL_QUERY_ID:
            print("  ⚠️  TweetDetail query ID missing; set api_config.tweet_detail_query_id")
            return {}

        url = f"https://x.com/i/api/graphql/{self.TWEET_DETAIL_QUERY_ID}/TweetDetail"
        variables = {
            "focalTweetId": str(tweet_id),
            "with_rux_injections": False,
            "rankingMode": "Relevance",
            "includePromotedContent": True,
            "withCommunity": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withBirdwatchNotes": True,
            "withVoice": True,
        }
        params = {
            "variables": self._compact_json(variables),
            "features": self._compact_json(
                {
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
            ),
            "fieldToggles": self._compact_json({"withArticleRichContentState": False}),
        }

        try:
            return self._request_with_context(
                url=url,
                params=params,
                referer=f"https://x.com/i/status/{tweet_id}",
                active_user="yes",
            )
        except Exception as e:
            print(f"  ✗ TweetDetail request failed for {tweet_id}: {e}")
            return {}

    # ------------------------------------------------------------------
    # Parse & Classify
    # ------------------------------------------------------------------

    def _parse_tweet(self, tweet_obj: Dict, conversation_context: Optional[Dict] = None) -> Optional[Dict]:
        """Parse a tweet and classify it as tweet/retweet/reply/quote."""
        # Handle tombstone/unavailable tweets
        typename = tweet_obj.get("__typename", "")
        if typename in ["TweetTombstone", "TweetUnavailable"]:
            return None
        
        try:
            legacy = tweet_obj.get("legacy", {})
            text = self._tweet_full_text(tweet_obj)
            
            # Extract user info from core (not legacy)
            user_result = (
                tweet_obj.get("core", {})
                .get("user_results", {})
                .get("result", {})
            )
            user_core = user_result.get("core", {})

            tweet_id = legacy.get("id_str", "")
            timestamp = legacy.get("created_at", "")

            # Base tweet data
            tweet = {
                "id": tweet_id,
                "text": text,
                "timestamp": timestamp,
                "jalali_time": self._to_jalali_datetime(timestamp),
                "jalali_date": self._to_jalali_date(timestamp),
                "link": f"https://x.com/i/status/{tweet_id}",
                "author": user_core.get("name", "Unknown"),
                "handle": "@" + user_core.get("screen_name", "unknown"),
                "engagement": {
                    "replies": str(legacy.get("reply_count", 0)),
                    "retweets": str(legacy.get("retweet_count", 0)),
                    "likes": str(legacy.get("favorite_count", 0)),
                    "views": str(tweet_obj.get("views", {}).get("count", 0) if isinstance(tweet_obj.get("views"), dict) else 0),
                    "quotes": str(legacy.get("quote_count", 0)),
                    "bookmarks": str(legacy.get("bookmark_count", 0)),
                },
                # NEW: Extract entities
                "entities": self._extract_entities(legacy),
            }

            # Classify tweet type
            if text.startswith("RT @") or "retweeted_status_result" in legacy:
                tweet["type"] = "retweet"
                tweet["retweet_info"] = self._parse_retweet(legacy, timestamp)
            elif legacy.get("in_reply_to_status_id_str"):
                tweet["type"] = "reply"
                tweet["reply_info"] = {
                    "parent_id": legacy.get("in_reply_to_status_id_str"),
                    "parent_user": legacy.get("in_reply_to_screen_name"),
                }
                # Try to get parent from conversation context
                if conversation_context:
                    tweet["reply_info"]["parent_tweet"] = conversation_context
            elif legacy.get("quoted_status_id_str"):
                tweet["type"] = "quote"
                tweet["quote_info"] = self._parse_quoted_tweet(tweet_obj)
            else:
                tweet["type"] = "tweet"

            return tweet
        except Exception as e:
            print(f"  ⚠️  Parse error: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _extract_entities(self, legacy: Dict) -> Dict:
        """Extract URLs, hashtags, and mentions from tweet entities."""
        entities_data = legacy.get("entities", {})
        
        # Extract URLs
        urls = []
        for url in entities_data.get("urls", []):
            urls.append({
                "display": url.get("display_url", ""),
                "expanded": url.get("expanded_url", ""),
                "short": url.get("url", "")
            })
        
        # Extract hashtags
        hashtags = [tag.get("text", "") for tag in entities_data.get("hashtags", [])]
        
        # Extract mentions
        mentions = []
        for mention in entities_data.get("user_mentions", []):
            mentions.append({
                "name": mention.get("name", ""),
                "handle": mention.get("screen_name", "")
            })
        
        # Extract media count
        media_count = 0
        media_types = []
        if "extended_entities" in legacy:
            media = legacy["extended_entities"].get("media", [])
            media_count = len(media)
            media_types = [m.get("type", "unknown") for m in media]
        
        return {
            "urls": urls,
            "hashtags": hashtags,
            "mentions": mentions,
            "media_count": media_count,
            "media_types": media_types
        }

    def _short_tweet_summary(self, tweet: Dict) -> Dict:
        """Small parent/ancestor representation for reply context."""
        return {
            "id": tweet.get("id"),
            "type": tweet.get("type", "tweet"),
            "author": tweet.get("author", "Unknown"),
            "handle": tweet.get("handle", "@unknown"),
            "text": tweet.get("text", ""),
            "jalali_time": tweet.get("jalali_time", ""),
            "link": tweet.get("link", ""),
            "parent_id": tweet.get("reply_info", {}).get("parent_id"),
            "parent_user": tweet.get("reply_info", {}).get("parent_user"),
        }

    def _attach_conversation_context(self, tweets: List[Dict]) -> None:
        """Attach available ancestor chains to replies from timeline conversations."""
        by_id = {tweet.get("id"): tweet for tweet in tweets if tweet.get("id")}
        for tweet in tweets:
            if tweet.get("type") != "reply":
                continue

            reply_info = tweet.setdefault("reply_info", {})
            parent_id = reply_info.get("parent_id")
            chain = []
            seen = {tweet.get("id")}

            while parent_id and parent_id in by_id and parent_id not in seen:
                parent = by_id[parent_id]
                seen.add(parent_id)
                chain.append(self._short_tweet_summary(parent))
                parent_id = parent.get("reply_info", {}).get("parent_id")

            chain.reverse()
            if chain:
                reply_info["conversation_chain"] = chain
                reply_info["parent_tweet"] = chain[-1]

    def _is_account_timeline_item(self, tweet: Dict, account: str) -> bool:
        """Keep fetched account activity as primary records, not every context tweet."""
        account_handle = f"@{account}".lower()
        if tweet.get("handle", "").lower() == account_handle:
            return True
        if tweet.get("type") == "retweet" and tweet.get("handle", "").lower() == account_handle:
            return True
        return False

    def _primary_item_id(self, tweet: Dict) -> str:
        """Stable ID for the saved item itself, excluding quoted/replied context IDs."""
        if tweet.get("type") == "retweet":
            original_id = str(tweet.get("retweet_info", {}).get("original_id") or "")
            if original_id:
                return original_id
        tweet_id = str(tweet.get("id") or "")
        if tweet_id:
            return tweet_id
        if tweet.get("type") == "quote":
            return str(tweet.get("quote_info", {}).get("quoted_id") or "")
        return ""

    def _extract_primary_id_from_block(self, block: str) -> Optional[str]:
        """Read the primary item ID from an existing formatted output block."""
        marker = re.search(r"🆔 Tweet ID:\s*(\d+)", block)
        if marker:
            return marker.group(1)

        link = re.search(r"^🔗 https://x\.com/(?:i/status|[^/\s]+/status)/(\d+)", block, re.MULTILINE)
        if link:
            return link.group(1)

        if "🔁 RETWEET" in block:
            original_link = re.search(r"│ 🔗 https://x\.com/[^/\s]+/status/(\d+)", block)
            if original_link:
                return f"retweet:{original_link.group(1)}"

        return None

    def _split_existing_blocks(self, content: str) -> List[str]:
        """Split old output into record blocks while preserving formatted text."""
        if not content.strip():
            return []
        parts = re.split(r"(?:^|\n)(?:═{70}\n){2}", content)
        blocks = []
        for part in parts:
            cleaned = part.strip()
            if cleaned:
                blocks.append(cleaned + "\n\n" + SEP + "\n" + SEP + "\n\n")
        return blocks

    def _dedupe_existing_content(self, content: str) -> Tuple[str, Set[str], int]:
        """Deduplicate existing daily output and return primary IDs already present."""
        seen: Set[str] = set()
        deduped_blocks = []
        removed = 0

        for block in self._split_existing_blocks(content):
            primary_id = self._extract_primary_id_from_block(block)
            fallback_key = re.sub(r"\s+", " ", block).strip()
            key = primary_id or fallback_key
            if key in seen:
                removed += 1
                continue
            seen.add(key)
            deduped_blocks.append(block)

        primary_ids = {key for key in seen if re.fullmatch(r"\d+", key) or key.startswith("retweet:")}
        primary_ids.update(key.split("retweet:", 1)[1] for key in seen if key.startswith("retweet:"))
        return "".join(deduped_blocks), primary_ids, removed

    def _empty_retweet_info(self, retweet_timestamp: str) -> Dict:
        """Return empty retweet info structure."""
        return {
            "original_author": "Unknown",
            "original_name": "Unknown",
            "original_text": "",
            "original_id": None,
            "original_link": None,
            "original_timestamp": "",
            "original_jalali_time": "",
            "retweet_timestamp": retweet_timestamp,
            "retweet_jalali_time": self._to_jalali_datetime(retweet_timestamp) if retweet_timestamp else "",
            "media_links": [],
            "entities": {"urls": [], "hashtags": [], "mentions": [], "media_count": 0, "media_types": []},
            "engagement": {"replies": "0", "retweets": "0", "likes": "0"}
        }

    def _parse_retweet(self, legacy: Dict, retweet_timestamp: str) -> Dict:
        """Parse retweet to extract original author and full text from retweeted_status_result."""
        # Check for retweeted_status_result (contains full original tweet)
        if "retweeted_status_result" in legacy:
            try:
                rsr = legacy["retweeted_status_result"]

                rt_result = self._unwrap_tweet_result(rsr)
                if not rt_result:
                    return self._empty_retweet_info(retweet_timestamp)

                # Check for tombstone or unavailable tweets
                typename = rt_result.get("__typename", "")
                if typename in ["TweetTombstone", "TweetUnavailable"]:
                    print(f"  ⚠️  Retweeted tweet is {typename} (deleted/unavailable)")
                    return self._empty_retweet_info(retweet_timestamp)
                
                rt_legacy = rt_result.get("legacy", {})
                
                # Get original author from core
                rt_user = rt_result.get("core", {}).get("user_results", {}).get("result", {})
                rt_user_core = rt_user.get("core", {})
                
                original_author = rt_user_core.get("screen_name", "Unknown")
                original_name = rt_user_core.get("name", "Unknown")
                original_text = self._tweet_full_text(rt_result)
                original_id = rt_legacy.get("id_str", "")
                original_timestamp = rt_legacy.get("created_at", "")
                
                # Check for media
                media_links = []
                if "extended_entities" in rt_legacy:
                    media = rt_legacy["extended_entities"].get("media", [])
                    for m in media:
                        if m.get("type") == "video":
                            media_links.append(m.get("expanded_url", ""))
                        elif m.get("type") == "photo":
                            media_links.append(m.get("media_url_https", ""))
                
                # Extract entities from original tweet
                original_entities = self._extract_entities(rt_legacy)
                
                return {
                    "original_author": f"@{original_author}",
                    "original_name": original_name,
                    "original_text": original_text,
                    "original_id": original_id,
                    "original_link": f"https://x.com/{original_author}/status/{original_id}" if original_id else None,
                    "original_timestamp": original_timestamp,
                    "original_jalali_time": self._to_jalali_datetime(original_timestamp) if original_timestamp else "",
                    "retweet_timestamp": retweet_timestamp,
                    "retweet_jalali_time": self._to_jalali_datetime(retweet_timestamp) if retweet_timestamp else "",
                    "media_links": media_links,
                    "entities": original_entities,
                    "engagement": {
                       "replies": str(rt_legacy.get("reply_count", 0)),
                       "retweets": str(rt_legacy.get("retweet_count", 0)),
                       "likes": str(rt_legacy.get("favorite_count", 0)),
                        "quotes": str(rt_legacy.get("quote_count", 0)),
                        "bookmarks": str(rt_legacy.get("bookmark_count", 0)),
                        "views": str(rt_result.get("views", {}).get("count", 0) if isinstance(rt_result.get("views"), dict) else 0),
                   }
                }
            except Exception as e:
                print(f"  ⚠️  Error parsing retweeted_status_result: {e}")
        
        # Fallback: parse from RT @ text
        text = legacy.get("full_text", "")
        match = re.match(r"RT @(\w+): (.+)", text, re.DOTALL)
        if match:
            original_author = match.group(1)
            original_text = match.group(2)
            
            return {
                "original_author": f"@{original_author}",
                "original_name": "Unknown",
                "original_text": original_text,
                "original_id": None,
                "original_link": None,
                "original_timestamp": "",
                "original_jalali_time": "",
                "retweet_timestamp": retweet_timestamp,
                "retweet_jalali_time": self._to_jalali_datetime(retweet_timestamp) if retweet_timestamp else "",
                "media_links": [],
                "entities": {"urls": [], "hashtags": [], "mentions": [], "media_count": 0, "media_types": []},
                "engagement": {"replies": "0", "retweets": "0", "likes": "0"}
            }
        
        # Final fallback - return empty with original text if available
        empty_info = self._empty_retweet_info(retweet_timestamp)
        if text:
            empty_info["original_text"] = text
        return empty_info

    def _empty_quote_info(self) -> Dict:
        """Return empty quote info structure."""
        return {
            "quoted_author": "Unknown",
            "quoted_name": "Unknown",
            "quoted_text": "",
            "quoted_id": None,
            "quoted_link": None,
            "quoted_timestamp": "",
            "quoted_jalali_time": "",
            "media_links": [],
            "entities": {"urls": [], "hashtags": [], "mentions": [], "media_count": 0, "media_types": []},
            "engagement": {"replies": "0", "retweets": "0", "likes": "0"}
        }

    def _unavailable_quote_info(self, quoted_status_id: str, reason: str) -> Dict:
        empty = self._empty_quote_info()
        if quoted_status_id:
            empty["quoted_id"] = quoted_status_id
            empty["quoted_link"] = f"https://x.com/i/status/{quoted_status_id}"
        empty["quoted_name"] = "[Unavailable]"
        empty["quoted_author"] = "[Unavailable]"
        empty["quoted_text"] = f"[Quoted tweet unavailable: {reason}]"
        return empty

    def _parse_quoted_tweet(self, tweet_obj: Dict) -> Dict:
        """Parse quoted tweet to extract full context."""
        legacy = tweet_obj.get("legacy", {})
        
        # Get quoted status ID even if full data not available
        quoted_status_id = legacy.get("quoted_status_id_str", "")
        
        if "quoted_status_result" in tweet_obj:
            try:
                qsr = tweet_obj["quoted_status_result"]

                quoted = self._unwrap_tweet_result(qsr)
                if not quoted:
                    empty = self._empty_quote_info()
                    if quoted_status_id:
                        empty["quoted_id"] = quoted_status_id
                        empty["quoted_link"] = f"https://x.com/i/status/{quoted_status_id}"
                        empty["quoted_text"] = "[Quoted tweet data not available from API]"
                        empty["quoted_name"] = "[Unknown]"
                        empty["quoted_author"] = "[Unknown]"
                    return empty
                
                # Check for tombstone or unavailable tweets
                typename = quoted.get("__typename", "")
                if typename in ["TweetTombstone", "TweetUnavailable"]:
                    return self._unavailable_quote_info(quoted_status_id, typename)
                
                quoted_legacy = quoted.get("legacy", {})
                
                # Get quoted author
                quoted_user = quoted.get("core", {}).get("user_results", {}).get("result", {})
                quoted_user_core = quoted_user.get("core", {})
                
                quoted_author = quoted_user_core.get("screen_name", "Unknown")
                quoted_name = quoted_user_core.get("name", "Unknown")
                quoted_text = self._tweet_full_text(quoted)
                quoted_id = quoted_legacy.get("id_str", "")
                quoted_timestamp = quoted_legacy.get("created_at", "")
                
                # Check for media in quoted tweet
                media_links = []
                if "extended_entities" in quoted_legacy:
                    media = quoted_legacy["extended_entities"].get("media", [])
                    for m in media:
                        if m.get("type") == "video":
                            media_links.append(m.get("expanded_url", ""))
                        elif m.get("type") == "photo":
                            media_links.append(m.get("media_url_https", ""))
                
                # Extract entities from quoted tweet
                quoted_entities = self._extract_entities(quoted_legacy)
                
                return {
                    "quoted_author": f"@{quoted_author}",
                    "quoted_name": quoted_name,
                    "quoted_text": quoted_text,
                    "quoted_id": quoted_id,
                    "quoted_link": f"https://x.com/{quoted_author}/status/{quoted_id}" if quoted_id else None,
                    "quoted_timestamp": quoted_timestamp,
                    "quoted_jalali_time": self._to_jalali_datetime(quoted_timestamp) if quoted_timestamp else "",
                    "media_links": media_links,
                    "entities": quoted_entities,
                    "engagement": {
                        "replies": str(quoted_legacy.get("reply_count", 0)),
                        "retweets": str(quoted_legacy.get("retweet_count", 0)),
                        "likes": str(quoted_legacy.get("favorite_count", 0)),
                    }
                }
            except Exception as e:
                print(f"  ⚠️  Error parsing quoted tweet: {e}")
        
        # If quoted_status_result not available, return minimal info with ID
        empty = self._empty_quote_info()
        if quoted_status_id:
            empty["quoted_id"] = quoted_status_id
            empty["quoted_link"] = f"https://x.com/i/status/{quoted_status_id}"
            empty["quoted_text"] = "[Quoted tweet data not available from API]"
            empty["quoted_name"] = "[Unknown]"
            empty["quoted_author"] = "[Unknown]"
        
        return empty

    # ------------------------------------------------------------------
    # Format
    # ------------------------------------------------------------------

    def _format_tweet(self, t: Dict) -> str:
        """Format an original tweet."""
        lines = ["💬 TWEET", ""]
        lines.append(f"👤 {t['author']} ({t['handle']})")
        lines.append(f"📅 {t['jalali_time']}")
        lines.append("")
        
        expanded_text = self._expand_urls_in_text(t["text"], t.get("entities", {}))
        lines.append(self._wrap(expanded_text))
        lines.append("")
        
        # Add entities
        lines.extend(self._format_entities(t.get("entities", {})))
        
        lines.append(f"🔗 {t['link']}")
        lines.append(f"🆔 Tweet ID: {t['id']}")
        lines.append("")
        lines.append(
            f"💬 {t['engagement']['replies']}  "
            f"🔁 {t['engagement']['retweets']}  "
            f"❤️ {t['engagement']['likes']}  "
            f"💬 {t['engagement']['quotes']}  "
            f"🔖 {t['engagement']['bookmarks']}  "
            f"👁 {t['engagement']['views']}"
        )
        lines.extend(["", SEP, SEP, ""])
        return "\n".join(lines)
    
    def _format_entities(self, entities: Dict) -> list:
        """Format URLs, hashtags, mentions, and media count."""
        lines = []
        
        # External URLs
        urls = entities.get("urls", [])
        if urls:
            lines.append("🔗 Links:")
            for url in urls:
                if url.get("expanded"):
                    lines.append(f"   → {url['expanded']}")
            lines.append("")
        
        # Media count
        media_count = entities.get("media_count", 0)
        media_types = entities.get("media_types", [])
        if media_count > 0:
            media_str = ", ".join(media_types)
            lines.append(f"📎 Media: {media_count} item(s) - {media_str}")
            lines.append("")
        
        # Hashtags
        hashtags = entities.get("hashtags", [])
        if hashtags:
            tags_str = " ".join([f"#{tag}" for tag in hashtags])
            lines.append(f"🏷️  {tags_str}")
            lines.append("")
        
        # Mentions
        mentions = entities.get("mentions", [])
        if mentions:
            lines.append("👥 Mentions:")
            for mention in mentions:
                lines.append(f"   @{mention['handle']} ({mention['name']})")
            lines.append("")
        
        return lines

    def _format_retweet(self, t: Dict) -> str:
        """Format a retweet with full original context."""
        rt_info = t.get("retweet_info", {})
        lines = ["🔁 RETWEET", ""]
        
        # Retweeter info (parent - the person who retweeted)
        lines.append(f"👤 Retweeted by: {t['author']} ({t['handle']})")
        retweet_time = rt_info.get("retweet_jalali_time", "")
        if retweet_time:
            lines.append(f"📅 Retweeted at: {retweet_time}")
        lines.append("")
        
        # Visual separator for original tweet (child/nested content)
        lines.append("┌" + "─" * 68 + "┐")
        lines.append("│ ORIGINAL TWEET" + " " * 53 + "│")
        lines.append("├" + "─" * 68 + "┤")
        
        # Show original author with name
        original_name = rt_info.get('original_name', 'Unknown')
        original_handle = rt_info.get('original_author', 'Unknown')
        lines.append(f"│ 👤 {original_name} ({original_handle})" + " " * (68 - len(f"│ 👤 {original_name} ({original_handle})")) + "│")
        
        # Original timestamp
        original_time = rt_info.get("original_jalali_time", "")
        if original_time:
            lines.append(f"│ 📅 {original_time}" + " " * (68 - len(f"│ 📅 {original_time}")) + "│")
        lines.append("│" + " " * 68 + "│")
        
        # Full original text
        expanded_text = self._expand_urls_in_text(rt_info.get("original_text", ""), rt_info.get("entities", {}))
        for line in expanded_text.split('\n'):
            wrapped_lines = textwrap.wrap(line, width=66) if line else ['']
            for wl in wrapped_lines:
                lines.append(f"│ {wl}" + " " * (68 - len(f"│ {wl}")) + "│")
        lines.append("│" + " " * 68 + "│")
        
        # Add entities from original tweet
        entity_lines = self._format_entities(rt_info.get("entities", {}))
        for eline in entity_lines:
            if eline:  # Skip empty lines
                lines.append(f"│ {eline}" + " " * (68 - len(f"│ {eline}")) + "│")
        
        # Media links if present
        media_links = rt_info.get("media_links", [])
        if media_links:
            lines.append("│" + " " * 68 + "│")
            for media_url in media_links:
                media_line = f"📎 {media_url}"
                lines.append(f"│ {media_line}" + " " * (68 - len(f"│ {media_line}")) + "│")
        
        # Original tweet link
        lines.append("│" + " " * 68 + "│")
        if rt_info.get("original_link"):
            link_line = f"🔗 {rt_info['original_link']}"
            lines.append(f"│ {link_line}" + " " * (68 - len(f"│ {link_line}")) + "│")
        if rt_info.get("original_id"):
            id_line = f"🆔 Tweet ID: {rt_info['original_id']}"
            lines.append(f"│ {id_line}" + " " * (68 - len(f"│ {id_line}")) + "│")
        
        lines.append("│" + " " * 68 + "│")
        
        # Show original engagement if available
        rt_engagement = rt_info.get("engagement", {})
        engagement_line = (
            f"💬 {rt_engagement.get('replies', '0')}  "
            f"🔁 {rt_engagement.get('retweets', '0')}  "
            f"❤️ {rt_engagement.get('likes', '0')}  "
            f"💬 {rt_engagement.get('quotes', '0')}  "
            f"🔖 {rt_engagement.get('bookmarks', '0')}  "
            f"👁 {rt_engagement.get('views', '0')}"
        )
        lines.append(f"│ {engagement_line}" + " " * (68 - len(f"│ {engagement_line}")) + "│")
        
        lines.append("└" + "─" * 68 + "┘")
        lines.extend(["", SEP, SEP, ""])
        return "\n".join(lines)

    def _format_reply(self, t: Dict) -> str:
        """Format a reply with parent context."""
        reply_info = t.get("reply_info", {})
        lines = ["↩️ REPLY", ""]
        
        # Reply author info (parent - the person replying)
        lines.append(f"👤 Reply by: {t['author']} ({t['handle']})")
        lines.append(f"📅 {t['jalali_time']}")
        lines.append("")
        
        # Reply text
        expanded_text = self._expand_urls_in_text(t["text"], t.get("entities", {}))
        lines.append(self._wrap(expanded_text))
        lines.append("")
        
        # Reply entities
        lines.extend(self._format_entities(t.get("entities", {})))
        
        # Reply link
        lines.append(f"🔗 {t['link']}")
        lines.append(f"🆔 Tweet ID: {t['id']}")
        lines.append("")
        
        # Reply engagement
        lines.append(
            f"💬 {t['engagement']['replies']}  "
            f"🔁 {t['engagement']['retweets']}  "
            f"❤️ {t['engagement']['likes']}  "
            f"💬 {t['engagement']['quotes']}  "
            f"🔖 {t['engagement']['bookmarks']}  "
            f"👁 {t['engagement']['views']}"
        )
        lines.append("")
        
        chain = reply_info.get("conversation_chain", [])
        if chain:
            lines.append("┌" + "─" * 68 + "┐")
            lines.append("│ CONVERSATION CHAIN" + " " * 49 + "│")
            lines.append("├" + "─" * 68 + "┤")
            for idx, node in enumerate(chain, start=1):
                role = "Direct parent" if idx == len(chain) else f"Ancestor {idx}"
                author_line = f"{role}: {node.get('author')} ({node.get('handle')})"
                lines.append(f"│ {author_line}" + " " * max(0, 68 - len(f"│ {author_line}")) + "│")
                if node.get("jalali_time"):
                    time_line = f"📅 {node['jalali_time']}"
                    lines.append(f"│ {time_line}" + " " * max(0, 68 - len(f"│ {time_line}")) + "│")
                text = self._expand_urls_in_text(node.get("text", ""), {})
                for line in text.split("\n"):
                    for wl in textwrap.wrap(line, width=66) if line else [""]:
                        lines.append(f"│ {wl}" + " " * max(0, 68 - len(f"│ {wl}")) + "│")
                if node.get("link"):
                    link_line = f"🔗 {node['link']}"
                    lines.append(f"│ {link_line}" + " " * max(0, 68 - len(f"│ {link_line}")) + "│")
                if node.get("id"):
                    id_line = f"🆔 Tweet ID: {node['id']}"
                    lines.append(f"│ {id_line}" + " " * max(0, 68 - len(f"│ {id_line}")) + "│")
                if idx != len(chain):
                    lines.append("│" + " " * 68 + "│")
                    lines.append("│ ↓ replies to" + " " * 56 + "│")
                    lines.append("│" + " " * 68 + "│")
            lines.append("└" + "─" * 68 + "┘")
            lines.append("")

        # Parent tweet section
        lines.append("┌" + "─" * 68 + "┐")
        lines.append("│ IN REPLY TO" + " " * 56 + "│")
        lines.append("├" + "─" * 68 + "┤")
        
        parent_tweet = reply_info.get("parent_tweet", {})
        parent_user = reply_info.get('parent_user', 'unknown')
        if parent_tweet:
            parent_label = f"{parent_tweet.get('author', 'Unknown')} ({parent_tweet.get('handle', '@unknown')})"
        else:
            parent_label = f"@{parent_user}"
        lines.append(f"│ 👤 {parent_label}" + " " * max(0, 68 - len(f"│ 👤 {parent_label}")) + "│")
        lines.append("│" + " " * 68 + "│")

        if parent_tweet.get("text"):
            parent_text = self._expand_urls_in_text(parent_tweet.get("text", ""), {})
            for line in parent_text.split("\n"):
                for wl in textwrap.wrap(line, width=66) if line else [""]:
                    lines.append(f"│ {wl}" + " " * max(0, 68 - len(f"│ {wl}")) + "│")
            lines.append("│" + " " * 68 + "│")
        
        parent_id = reply_info.get("parent_id")
        if parent_id:
            link_line = parent_tweet.get("link") or f"🔗 https://x.com/{parent_user}/status/{parent_id}"
            if not link_line.startswith("🔗"):
                link_line = f"🔗 {link_line}"
            lines.append(f"│ {link_line}" + " " * (68 - len(f"│ {link_line}")) + "│")
            id_line = f"🆔 Tweet ID: {parent_id}"
            lines.append(f"│ {id_line}" + " " * max(0, 68 - len(f"│ {id_line}")) + "│")
        
        lines.append("└" + "─" * 68 + "┘")
        lines.extend(["", SEP, SEP, ""])
        return "\n".join(lines)

    def _format_quote(self, t: Dict) -> str:
        """Format a quote tweet."""
        quote_info = t.get("quote_info", {})
        lines = ["📎 QUOTE TWEET", ""]
        
        # Quote tweet author (parent - person quoting)
        lines.append(f"👤 Quote by: {t['author']} ({t['handle']})")
        lines.append(f"📅 {t['jalali_time']}")
        lines.append("")
        
        # User's comment
        expanded_text = self._expand_urls_in_text(t["text"], t.get("entities", {}))
        lines.append(self._wrap(expanded_text))
        lines.append("")
        
        # Add entities from user's comment
        lines.extend(self._format_entities(t.get("entities", {})))
        
        # User's tweet link
        lines.append(f"🔗 {t['link']}")
        lines.append(f"🆔 Tweet ID: {t['id']}")
        lines.append("")
        
        # User's engagement
        lines.append(
            f"💬 {t['engagement']['replies']}  "
            f"🔁 {t['engagement']['retweets']}  "
            f"❤️ {t['engagement']['likes']}  "
            f"💬 {t['engagement']['quotes']}  "
            f"🔖 {t['engagement']['bookmarks']}  "
            f"👁 {t['engagement']['views']}"
        )
        lines.append("")
        
        # Quoted tweet section
        lines.append("┌" + "─" * 68 + "┐")
        lines.append("│ QUOTED TWEET" + " " * 55 + "│")
        lines.append("├" + "─" * 68 + "┤")
        
        # Quoted tweet author
        quoted_name = quote_info.get("quoted_name", "Unknown")
        quoted_handle = quote_info.get("quoted_author", "Unknown")
        
        # Check if data is unavailable
        is_unavailable = quoted_name == "[Unknown]" or "not available" in quote_info.get("quoted_text", "")
        
        if is_unavailable:
            unavail_line = "│ ⚠️  Quoted tweet data not available from API" + " " * 23 + "│"
            lines.append(unavail_line)
        else:
            lines.append(f"│ 👤 {quoted_name} ({quoted_handle})" + " " * (68 - len(f"│ 👤 {quoted_name} ({quoted_handle})")) + "│")
        
        # Quoted tweet timestamp
        quoted_time = quote_info.get("quoted_jalali_time", "")
        if quoted_time and not is_unavailable:
            lines.append(f"│ 📅 {quoted_time}" + " " * (68 - len(f"│ 📅 {quoted_time}")) + "│")
        
        if not is_unavailable:
            lines.append("│" + " " * 68 + "│")
        
        # Quoted tweet text
        quoted_text = quote_info.get("quoted_text", "")
        if quoted_text and not is_unavailable:
            expanded_quoted_text = self._expand_urls_in_text(quoted_text, quote_info.get("entities", {}))
            for line in expanded_quoted_text.split('\n'):
                wrapped_lines = textwrap.wrap(line, width=66) if line else ['']
                for wl in wrapped_lines:
                    lines.append(f"│ {wl}" + " " * (68 - len(f"│ {wl}")) + "│")
            lines.append("│" + " " * 68 + "│")
        
        # Quoted tweet entities
        entity_lines = self._format_entities(quote_info.get("entities", {})) if not is_unavailable else []
        for eline in entity_lines:
            if eline:  # Skip empty lines
                lines.append(f"│ {eline}" + " " * (68 - len(f"│ {eline}")) + "│")
        
        # Quoted tweet media
        quoted_media = quote_info.get("media_links", [])
        if quoted_media and not is_unavailable:
            lines.append("│" + " " * 68 + "│")
            for media_url in quoted_media:
                media_line = f"📎 {media_url}"
                lines.append(f"│ {media_line}" + " " * (68 - len(f"│ {media_line}")) + "│")
        
        # Quoted tweet link
        lines.append("│" + " " * 68 + "│")
        lines.append("│" + " " * 68 + "│")
        quoted_link = quote_info.get("quoted_link")
        if quoted_link:
            link_line = f"🔗 {quoted_link}"
            lines.append(f"│ {link_line}" + " " * (68 - len(f"│ {link_line}")) + "│")
        if quote_info.get("quoted_id"):
            id_line = f"🆔 Tweet ID: {quote_info['quoted_id']}"
            lines.append(f"│ {id_line}" + " " * max(0, 68 - len(f"│ {id_line}")) + "│")
        
        # Quoted tweet engagement
        quoted_engagement = quote_info.get("engagement", {})
        if quoted_engagement and not is_unavailable:
            lines.append("│" + " " * 68 + "│")
            engagement_line = (
                f"💬 {quoted_engagement.get('replies', '0')}  "
                f"🔁 {quoted_engagement.get('retweets', '0')}  "
                f"❤️ {quoted_engagement.get('likes', '0')}  "
                f"💬 {quoted_engagement.get('quotes', '0')}  "
                f"🔖 {quoted_engagement.get('bookmarks', '0')}"
            )
            lines.append(f"│ {engagement_line}" + " " * (68 - len(f"│ {engagement_line}")) + "│")
        
        lines.append("└" + "─" * 68 + "┘")
        lines.extend(["", SEP, SEP, ""])
        return "\n".join(lines)

    def _format_item(self, tweet: Dict) -> str:
        """Format a tweet based on its type."""
        tweet_type = tweet.get("type", "tweet")
        if tweet_type == "retweet":
            return self._format_retweet(tweet)
        elif tweet_type == "reply":
            return self._format_reply(tweet)
        elif tweet_type == "quote":
            return self._format_quote(tweet)
        else:
            return self._format_tweet(tweet)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save_to_daily_file(self, account: str, tweets: List[Dict]):
       """Save tweets to unified daily files, sorted by time."""
       account_folder = self.tweets_root / account.upper()
       account_folder.mkdir(exist_ok=True)

       self._attach_conversation_context(tweets)
       tweets = [tweet for tweet in tweets if self._is_account_timeline_item(tweet, account)]
       if not tweets:
           return

       # Group by date
       by_date: Dict[str, List[Dict]] = {}
       for tweet in tweets:
           date = tweet["jalali_date"]
           if date not in by_date:
               by_date[date] = []
           by_date[date].append(tweet)

       # Save each date file
       for date, date_tweets in by_date.items():
           file_path = account_folder / f"{date}.txt"
           
           # Load existing content and IDs to avoid duplicates
           existing_ids: Set[str] = set()
           existing_content = ""
           if file_path.exists():
               existing_content = file_path.read_text(encoding="utf-8")
               existing_content, existing_ids, removed = self._dedupe_existing_content(existing_content)
               if removed:
                   print(f"    ✓ Removed {removed} duplicate existing item(s) from {date}.txt")
           
           # Filter out duplicates
           new_tweets = [
               t for t in date_tweets
               if self._primary_item_id(t) and self._primary_item_id(t) not in existing_ids
           ]
           
           if not new_tweets:
               if file_path.exists() and existing_content != file_path.read_text(encoding="utf-8"):
                   file_path.write_text(existing_content, encoding="utf-8")
               continue
           
           # Sort by timestamp (newest first)
           new_tweets.sort(
               key=lambda t: self._parse_timestamp(t["timestamp"]) or datetime.min,
               reverse=True,
           )
           
           # Format all tweets
           formatted = [self._format_item(t) for t in new_tweets]
           
           # Prepend new tweets to existing content
           new_content = "".join(formatted)
           if existing_content:
               new_content += existing_content
           
           file_path.write_text(new_content, encoding="utf-8")
           print(f"    ✓ Saved {len(new_tweets)} items to {date}.txt")

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def fetch_account(self, username: str) -> bool:
        """Fetch all tweets, retweets, and replies for an account."""
        print(f"  📥 Fetching data...")
        
        user_id = self.get_user_id(username)
        if not user_id:
            return False

        all_tweets: Dict[str, Dict] = {}  # tweet_id -> tweet_data
        
        # Fetch from UserTweets endpoint
        print(f"  📄 Fetching tweets + retweets...")
        cursor = None
        page = 1
        
        while True:
            data = self.get_user_tweets(user_id, cursor)
            if not data:
                break
            
            instructions = (
                data.get("data", {})
                .get("user", {})
                .get("result", {})
                .get("timeline", {})
                .get("timeline", {})
                .get("instructions", [])
            )
            
            found_entries = False
            next_cursor = None
            
            for inst in instructions:
                if inst.get("type") == "TimelineAddEntries":
                    entries = inst.get("entries", [])
                    found_entries = True
                    
                    for entry in entries:
                        entry_id = entry.get("entryId", "")
                        
                        # Handle cursor
                        if entry_id.startswith("cursor-bottom-"):
                            next_cursor = entry.get("content", {}).get("value")
                            continue
                        
                        # Handle tweet entries
                        if entry_id.startswith("tweet-"):
                            content = entry.get("content", {})
                            item_content = content.get("itemContent", {})
                            tweet_results = item_content.get("tweet_results", {})
                            tweet_obj = tweet_results.get("result", {})
                            
                            if tweet_obj:
                                parsed = self._parse_tweet(tweet_obj)
                                if parsed and parsed.get("id"):
                                    # Check timeframe
                                    if not self._is_within_timeframe(parsed["timestamp"]):
                                        print(f"  ✓ Reached 2-week limit")
                                        next_cursor = None
                                        break
                                    
                                    all_tweets[parsed["id"]] = parsed
                    
                    if not next_cursor:
                        break
            
            if not found_entries or not next_cursor:
                break
            
            cursor = next_cursor
            page += 1
            print(f"    Page {page}: {len(all_tweets)} items so far")
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        
        print(f"  ✓ UserTweets: {len(all_tweets)} items")
        
        # Fetch from UserTweetsAndReplies endpoint
        if FETCH_REPLIES:
            print(f"  📄 Fetching replies...")
        else:
            print(f"  ⏭️  Skipping replies (endpoint disabled)")
            # Save and return
            print(f"  ✓ Total unique items: {len(all_tweets)}")
            if all_tweets:
                self._save_to_daily_file(username, list(all_tweets.values()))
            print(f"  ✅ Complete")
            return True
        
        cursor = None
        page = 1
        
        while True:
            data = self.get_user_tweets_and_replies(user_id, cursor, username)
            if not data:
                break
            
            instructions = (
                data.get("data", {})
                .get("user", {})
                .get("result", {})
                .get("timeline", {})
                .get("timeline", {})
                .get("instructions", [])
            )
            
            found_entries = False
            next_cursor = None
            
            for inst in instructions:
                if inst.get("type") == "TimelineAddEntries":
                    entries = inst.get("entries", [])
                    found_entries = True
                    
                    for entry in entries:
                        entry_id = entry.get("entryId", "")
                        
                        # Handle cursor
                        if entry_id.startswith("cursor-bottom-"):
                            next_cursor = entry.get("content", {}).get("value")
                            continue
                        
                        # Handle tweet entries
                        if entry_id.startswith("tweet-"):
                            content = entry.get("content", {})
                            item_content = content.get("itemContent", {})
                            tweet_results = item_content.get("tweet_results", {})
                            tweet_obj = tweet_results.get("result", {})
                            
                            if tweet_obj:
                                parsed = self._parse_tweet(tweet_obj)
                                if parsed and parsed.get("id"):
                                    if not self._is_within_timeframe(parsed["timestamp"]):
                                        next_cursor = None
                                        break
                                    
                                    # Add or update (replies endpoint has priority for reply context)
                                    all_tweets[parsed["id"]] = parsed
                        
                        # Handle conversation threads (for reply context)
                        elif entry_id.startswith("profile-conversation-"):
                            content = entry.get("content", {})
                            items = content.get("items", [])
                            
                            # Extract parent and reply from conversation
                            for item_entry in items:
                                item_content = item_entry.get("item", {}).get("itemContent", {})
                                tweet_results = item_content.get("tweet_results", {})
                                tweet_obj = tweet_results.get("result", {})
                                
                                if tweet_obj:
                                    parsed = self._parse_tweet(tweet_obj)
                                    if parsed and parsed.get("id"):
                                        if not self._is_within_timeframe(parsed["timestamp"]):
                                            continue
                                        
                                        all_tweets[parsed["id"]] = parsed
                    
                    if not next_cursor:
                        break
            
            if not found_entries or not next_cursor:
                break
            
            cursor = next_cursor
            page += 1
            print(f"    Page {page}: {len(all_tweets)} total items")
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        
        print(f"  ✓ Total unique items: {len(all_tweets)}")
        
        # Save to files
        if all_tweets:
            self._save_to_daily_file(username, list(all_tweets.values()))
        
        print(f"  ✅ Complete")
        return True


# ──────────────────────────────────────────────────────────────────────────────

def main():
    os.chdir(Path(__file__).parent)

    print("=" * 70)
    print("Twitter Historical Tweet Fetcher - Unified Timeline")
    print("=" * 70)

    fetcher = TwitterUnifiedFetcher()

    if not ACCOUNTS:
        print("\n⚠️  No accounts configured — edit ACCOUNTS at top of script.")
        sys.exit(1)

    print(f"\n📋 Processing {len(ACCOUNTS)} account(s): {', '.join(ACCOUNTS)}\n")

    successful, failed = [], []

    for i, account in enumerate(ACCOUNTS, 1):
        print(f"\n[{i}/{len(ACCOUNTS)}] @{account}")
        try:
            ok = fetcher.fetch_account(account)
            (successful if ok else failed).append(account)
        except Exception as e:
            print(f"  ✗ Fatal error for @{account}: {e}")
            failed.append(account)

        if i < len(ACCOUNTS):
            time.sleep(random.uniform(3, 6))

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"✅ Successful: {len(successful)}/{len(ACCOUNTS)}")
    for a in successful:
        print(f"   ✓ @{a}")
    if failed:
        print(f"\n⚠️  Failed: {len(failed)}/{len(ACCOUNTS)}")
        for a in failed:
            print(f"   ✗ @{a}")
    print("\n✅ All done!")


if __name__ == "__main__":
    main()
