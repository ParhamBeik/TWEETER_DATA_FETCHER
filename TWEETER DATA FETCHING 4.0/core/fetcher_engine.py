#!/usr/bin/env python3
"""
Phase 2 Fetcher Engine

Implements:
- Human-like session warm-up before account fetching
- Strict sequential endpoint processing per account
- Hard-stop 4xx diagnostics with high-visibility debug output
- Cursor-aware pagination with explicit transitions
- Enhanced observability with rich (fallback to std logging)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from core.api_manager import APIManager
from data_pipeline.storage_manager import StorageManager
from config.tier_config import get_priority_policy, load_tier_config, ordered_accounts

try:
    import pytz
except ImportError:
    print("ERROR: Missing dependency pytz. Run: pip3 install pytz")
    raise

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
except Exception:  # pragma: no cover - fallback path
    Console = None
    Panel = None
    Table = None


TIMEZONE = "Asia/Tehran"
DEFAULT_HISTORICAL_MAX_PAGES = 15
SEP = "═" * 90


class EngineLogger:
    """Rich-first logger with plain fallback."""

    def __init__(self):
        self.rich_enabled = Console is not None
        self.console = Console() if self.rich_enabled else None

    def info(self, message: str):
        if self.rich_enabled:
            self.console.print(f"[bold cyan][INFO][/bold cyan] {message}")
        else:
            print(f"[INFO] {message}")

    def success(self, message: str):
        if self.rich_enabled:
            self.console.print(f"[bold green][OK][/bold green] {message}")
        else:
            print(f"[OK] {message}")

    def warning(self, message: str):
        if self.rich_enabled:
            self.console.print(f"[bold yellow][WARN][/bold yellow] {message}")
        else:
            print(f"[WARN] {message}")

    def error(self, message: str):
        if self.rich_enabled:
            self.console.print(f"[bold red][ERROR][/bold red] {message}")
        else:
            print(f"[ERROR] {message}")

    def banner(self, title: str, body: str):
        if self.rich_enabled and Panel is not None:
            self.console.print(Panel.fit(body, title=title, border_style="magenta"))
        else:
            print(SEP)
            print(title)
            print(SEP)
            print(body)
            print(SEP)

    def show_startup_config(self, config: Dict[str, Any], account_map: Dict[str, Dict], policies: Dict[int, Dict]):
        api_cfg = config.get("api_config", {})
        if self.rich_enabled and Table is not None:
            table = Table(title="Loaded API / Tier Configuration", show_lines=False)
            table.add_column("Key", style="cyan")
            table.add_column("Value", style="white")
            table.add_row("Config File", "config/config.json")
            table.add_row("Accounts (tiered)", str(len(account_map)))
            table.add_row("Priority Policies", str(len(policies)))
            table.add_row("UserByScreenName QueryID", str(api_cfg.get("user_by_screen_name_query_id", ""))[:20] + "...")
            table.add_row("UserTweets QueryID", str(api_cfg.get("user_tweets_query_id", ""))[:20] + "...")
            table.add_row("UserTweetsAndReplies QueryID", str(api_cfg.get("user_tweets_and_replies_query_id", ""))[:20] + "...")
            table.add_row("Timeout (sec)", str(api_cfg.get("default_timeout_seconds", 20)))
            self.console.print(table)
        else:
            self.info(f"Config File: config/config.json")
            self.info(f"Accounts (tiered): {len(account_map)}")
            self.info(f"Priority Policies: {len(policies)}")
            self.info(f"Timeout (sec): {api_cfg.get('default_timeout_seconds', 20)}")

    def pagination(self, account: str, endpoint: str, page: int, cursor: Optional[str]):
        cursor_text = cursor if cursor else "END"
        self.info(f"Account: @{account} | Endpoint: {endpoint} | Page: {page} | Next Cursor: {cursor_text}")


class FetcherEngine:
    """Phase 2 sequential fetcher with strict failure visibility."""

    def __init__(self, config_path: str = "config/config.json"):
        self.project_root = Path(__file__).resolve().parent.parent
        self.logger = EngineLogger()
        self.api_manager = APIManager(config_path=config_path, state_dir=self.project_root / "data" / "state")
        self.storage_manager = StorageManager(base_dir=self.project_root, timezone=TIMEZONE)

        self.config = self.api_manager.config
        self.tz = pytz.timezone(TIMEZONE)
        self.account_map, self.priority_policies = load_tier_config(self.config)
        self.max_cursor_error_retries = int(
            self.config.get("api_config", {}).get("cursor_error_max_retries", 3)
        )
        self.backoff_schedule_seconds = [15, 30, 60]

        self.logger.show_startup_config(self.config, self.account_map, self.priority_policies)

    @staticmethod
    def _compact_json(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    def _timeline_features(self) -> Dict[str, Any]:
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
            "rweb_cashtags_composer_attachment_enabled": True,
            "responsive_web_jetfuel_frame": True,
            "responsive_web_grok_share_attachment_enabled": True,
            "responsive_web_grok_annotations_enabled": True,
            "articles_preview_enabled": True,
            "responsive_web_edit_tweet_api_enabled": True,
            "rweb_conversational_replies_downvote_enabled": False,
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

    def _extract_bottom_cursor(self, payload: Dict[str, Any]) -> Optional[str]:
        instructions = (
            payload.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )

        for inst in instructions:
            if inst.get("type") != "TimelineAddEntries":
                continue
            for entry in inst.get("entries", []):
                entry_id = str(entry.get("entryId", ""))
                if "cursor-bottom" in entry_id:
                    value = entry.get("content", {}).get("value")
                    if value:
                        return str(value)
        return None

    def _extract_timeline_items(self, payload: Dict[str, Any], username: str) -> List[Dict[str, Any]]:
        instructions = (
            payload.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )

        items: List[Dict[str, Any]] = []
        for inst in instructions:
            if inst.get("type") != "TimelineAddEntries":
                continue
            for entry in inst.get("entries", []):
                entry_id = str(entry.get("entryId", ""))
                if not entry_id.startswith("tweet-"):
                    continue
                item_content = entry.get("content", {}).get("itemContent", {})
                tweet_result = item_content.get("tweet_results", {}).get("result", {})
                legacy = tweet_result.get("legacy", {})
                rest_id = tweet_result.get("rest_id")
                if not legacy or not rest_id:
                    continue
                created_at = legacy.get("created_at", "")
                items.append(
                    {
                        "id": str(rest_id),
                        "account": username,
                        "timestamp": created_at,
                        "text": legacy.get("full_text") or legacy.get("text") or "",
                        "url": f"https://x.com/{username}/status/{rest_id}",
                        "likes": legacy.get("favorite_count", 0),
                        "retweets": legacy.get("retweet_count", 0),
                        "replies": legacy.get("reply_count", 0),
                        "quotes": legacy.get("quote_count", 0),
                        "bookmarks": legacy.get("bookmark_count", 0),
                        "views": (tweet_result.get("views", {}) or {}).get("count", 0),
                        "type": "Tweet",
                    }
                )
        return items

    def _log_4xx_and_exit(
        self,
        *,
        account: str,
        endpoint: str,
        response,
        request_url: str,
        request_headers: Dict[str, Any],
        variables: Dict[str, Any],
        cursor: Optional[str],
    ):
        block = {
            "status_code": response.status_code,
            "account": account,
            "endpoint": endpoint,
            "request_url": request_url,
            "cursor": cursor,
            "headers": request_headers,
            "variables": variables,
            "response_text": response.text[:4000],
        }
        body = json.dumps(block, indent=2, ensure_ascii=False)
        self.logger.banner("CRITICAL 4xx ERROR - EXECUTION HALTED", body)
        raise SystemExit(1)

    def _log_4xx_details(
        self,
        *,
        account: str,
        endpoint: str,
        response,
        request_url: str,
        request_headers: Dict[str, Any],
        variables: Dict[str, Any],
        cursor: Optional[str],
        title: str = "CRITICAL 4xx ERROR",
    ) -> None:
        """Non-exiting version for cursor error handling with resume support."""
        block = {
            "status_code": response.status_code,
            "account": account,
            "endpoint": endpoint,
            "request_url": request_url,
            "cursor": cursor,
            "headers": request_headers,
            "variables": variables,
            "response_text": response.text[:4000],
        }
        body = json.dumps(block, indent=2, ensure_ascii=False)
        self.logger.banner(title, body)

    def _build_graphql_url(
        self,
        *,
        endpoint: str,
        query_id: str,
        variables: Dict[str, Any],
        features: Dict[str, Any],
        field_toggles: Optional[Dict[str, Any]] = None,
    ) -> str:
        base_url = f"https://x.com/i/api/graphql/{query_id}/{endpoint}"
        query_params = {
            "variables": self._compact_json(variables),
            "features": self._compact_json(features),
        }
        if field_toggles is not None:
            query_params["fieldToggles"] = self._compact_json(field_toggles)
        return f"{base_url}?{urlencode(query_params)}"

    def _get_user_id(self, username: str) -> str:
        query_id = self.api_manager.get_query_id("UserByScreenName")
        if not query_id:
            raise RuntimeError("Missing query ID for UserByScreenName")

        endpoint = "UserByScreenName"
        variables = {"screen_name": username, "withSafetyModeUserFields": True}
        features = {
            "hidden_profile_subscriptions_enabled": True,
            "rweb_tipjar_consumption_enabled": True,
        }

        request_url = self._build_graphql_url(
            endpoint=endpoint,
            query_id=query_id,
            variables=variables,
            features=features,
        )
        response = self.api_manager.perform_get(endpoint=endpoint, url=request_url, username=username)

        if response.status_code in {400, 401, 403, 404}:
            self._log_4xx_and_exit(
                account=username,
                endpoint=endpoint,
                response=response,
                request_url=request_url,
                request_headers=dict(response.request.headers),
                variables=variables,
                cursor=None,
            )

        response.raise_for_status()
        payload = response.json()
        user_id = (
            payload.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("rest_id")
        )
        if not user_id:
            raise RuntimeError(f"Could not resolve user id for @{username}")
        return str(user_id)

    def _fetch_endpoint_pages(
        self,
        *,
        account: str,
        user_id: str,
        endpoint: str,
        max_pages: int,
    ) -> List[Dict[str, Any]]:
        query_id = self.api_manager.get_query_id(endpoint)
        if not query_id:
            raise RuntimeError(f"Missing query ID for endpoint: {endpoint}")

        features = self._timeline_features()
        field_toggles = {"withArticlePlainText": False}
        existing_state = self.storage_manager.get_endpoint_state(account, endpoint)
        status_value = str(existing_state.get("status", "pending"))
        resume_cursor = existing_state.get("last_cursor")
        cursor: Optional[str] = (
            str(resume_cursor)
            if (
                resume_cursor
                and status_value in {"paused", "failed"}
                and str(resume_cursor) not in {"__START__", "__END__"}
            )
            else None
        )
        if cursor:
            self.logger.warning(
                f"Resuming @{account} {endpoint} from saved cursor: {cursor}"
            )

        # Mark active run state at loop start.
        self.storage_manager.update_endpoint_state(
            account,
            endpoint,
            last_cursor=cursor if cursor else None,
            status="running",
        )

        page = 1
        all_items: List[Dict[str, Any]] = []

        while page <= max_pages:
            variables: Dict[str, Any] = {
                "userId": user_id,
                "count": 20,
                "includePromotedContent": True,
            }
            if endpoint == "UserTweetsAndReplies":
                variables["withCommunity"] = True
                variables["withVoice"] = True
            else:
                variables["withQuickPromoteEligibilityTweetFields"] = True
                variables["withVoice"] = True

            if cursor:
                variables["cursor"] = cursor

            request_url = self._build_graphql_url(
                endpoint=endpoint,
                query_id=query_id,
                variables=variables,
                features=features,
                field_toggles=field_toggles,
            )

            request_headers: Dict[str, str] = {}
            if endpoint == "UserTweetsAndReplies":
                request_headers = {
                    "referer": f"https://x.com/{account}/with_replies",
                    "x-twitter-active-user": "yes",
                }
            elif endpoint == "UserTweets":
                request_headers = {
                    "referer": f"https://x.com/{account}",
                    "x-twitter-active-user": "yes",
                }

            response = None
            last_exception: Optional[Exception] = None
            page_request_succeeded = False

            for attempt in range(self.max_cursor_error_retries):
                try:
                    response = self.api_manager.perform_get(
                        endpoint=endpoint,
                        url=request_url,
                        username=account,
                        headers=request_headers,
                    )

                    if response.status_code in {400, 401, 403, 404, 429}:
                        self._log_4xx_details(
                            account=account,
                            endpoint=endpoint,
                            response=response,
                            request_url=request_url,
                            request_headers=dict(response.request.headers),
                            variables=variables,
                            cursor=cursor,
                            title=(
                                "CURSOR ERROR (RETRYING)"
                                if attempt < self.max_cursor_error_retries - 1
                                else "CURSOR ERROR (MAX RETRIES REACHED)"
                            ),
                        )
                        if attempt < self.max_cursor_error_retries - 1:
                            wait = self.backoff_schedule_seconds[min(attempt, len(self.backoff_schedule_seconds) - 1)]
                            self.logger.warning(
                                f"@{account} {endpoint} got HTTP {response.status_code}; retry in {wait}s "
                                f"(attempt {attempt + 1}/{self.max_cursor_error_retries})"
                            )
                            time.sleep(wait)
                            continue

                        # Max retries reached -> pause endpoint and continue outer workflow.
                        self.storage_manager.update_endpoint_state(
                            account,
                            endpoint,
                            last_cursor=cursor if cursor else "__START__",
                            status="paused",
                            meta={
                                "last_http_status": int(response.status_code),
                                "last_error_at": datetime.utcnow().isoformat() + "Z",
                            },
                        )
                        self.logger.warning(
                            f"Pausing @{account} {endpoint} after repeated HTTP {response.status_code}; "
                            "moving to next endpoint/account."
                        )
                        return all_items

                    response.raise_for_status()
                    page_request_succeeded = True
                    break
                except Exception as exc:
                    last_exception = exc
                    if attempt < self.max_cursor_error_retries - 1:
                        wait = self.backoff_schedule_seconds[min(attempt, len(self.backoff_schedule_seconds) - 1)]
                        self.logger.warning(
                            f"@{account} {endpoint} request error: {exc}; retry in {wait}s "
                            f"(attempt {attempt + 1}/{self.max_cursor_error_retries})"
                        )
                        time.sleep(wait)
                        continue

                    self.storage_manager.update_endpoint_state(
                        account,
                        endpoint,
                        last_cursor=cursor if cursor else "__START__",
                        status="failed",
                        meta={
                            "last_error": str(exc),
                            "last_error_at": datetime.utcnow().isoformat() + "Z",
                        },
                    )
                    self.logger.warning(
                        f"Marking @{account} {endpoint} as failed after max retries; moving on."
                    )
                    return all_items

            if not page_request_succeeded:
                # Defensive fallback, should be unreachable due to returns above.
                self.storage_manager.update_endpoint_state(
                    account,
                    endpoint,
                    last_cursor=cursor if cursor else "__START__",
                    status="failed",
                    meta={"last_error": str(last_exception) if last_exception else "unknown"},
                )
                return all_items

            if response is None:
                self.storage_manager.update_endpoint_state(
                    account,
                    endpoint,
                    last_cursor=cursor if cursor else "__START__",
                    status="failed",
                )
                return all_items

            payload = response.json()

            all_items.append(payload)
            next_cursor = self._extract_bottom_cursor(payload)

            self.storage_manager.update_endpoint_state(
                account,
                endpoint,
                last_cursor=next_cursor if next_cursor else "__END__",
                status="running",
                meta={
                    "last_page_fetched_at": datetime.utcnow().isoformat() + "Z",
                    "last_page_number": page,
                },
            )

            self.logger.pagination(account=account, endpoint=endpoint, page=page, cursor=next_cursor)
            if next_cursor:
                self.logger.info(
                    f"Page {page} fetched -> Cursor found: {next_cursor} -> Requesting Page {page + 1}"
                )
                cursor = next_cursor
                page += 1
                continue

            self.logger.info(
                f"Account: @{account} | Endpoint: {endpoint} | End of pagination reached"
            )
            self.storage_manager.update_endpoint_state(
                account,
                endpoint,
                last_cursor="__END__",
                status="completed",
                meta={"completed_at": datetime.utcnow().isoformat() + "Z"},
            )
            break

        if page > max_pages:
            self.storage_manager.update_endpoint_state(
                account,
                endpoint,
                last_cursor=cursor if cursor else "__END__",
                status="completed",
                meta={"completed_at": datetime.utcnow().isoformat() + "Z", "reason": "max_pages_reached"},
            )

        return all_items

    def _persist_endpoint_output(self, account: str, endpoint: str, tweets: List[Dict[str, Any]]):
        endpoint_map = {
            "UserTweets": self.storage_manager.user_tweets_dir,
            "UserTweetsAndReplies": self.storage_manager.user_replies_dir,
        }
        target_dir = endpoint_map[endpoint]

        by_date: Dict[str, List[Dict[str, Any]]] = {}
        extracted: List[Dict[str, Any]] = []
        for payload in tweets:
            extracted.extend(self._extract_timeline_items(payload, account))

        for tweet in extracted:
            created = tweet.get("timestamp") or ""
            date_str = None
            if created:
                try:
                    dt = datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y")
                    tehran_dt = dt.astimezone(self.tz)
                    date_str = tehran_dt.strftime("%Y-%m-%d")
                except Exception:
                    date_str = None
            if not date_str:
                date_str = (datetime.now(self.tz) - timedelta(days=0)).strftime("%Y-%m-%d")

            by_date.setdefault(date_str, []).append(tweet)

        for date_str, day_tweets in by_date.items():
            self.storage_manager.save_tweets_to_file(day_tweets, account, date_str, target_dir)

    def run(self, selected_accounts: Optional[List[str]] = None):
        accounts = selected_accounts or ordered_accounts(self.account_map)
        if not accounts:
            self.logger.warning("No accounts found in tier configuration.")
            return

        self.logger.info(f"Starting sequential fetch for {len(accounts)} account(s)")

        for idx, username in enumerate(accounts, start=1):
            self.storage_manager.ensure_account_state(username)
            policy = get_priority_policy(username, self.account_map, self.priority_policies)
            max_pages = int(policy.get("historical_max_pages", DEFAULT_HISTORICAL_MAX_PAGES))

            self.logger.banner(
                f"ACCOUNT {idx}/{len(accounts)}",
                f"@{username}\npriority={policy.get('priority')}\nmax_pages={max_pages}",
            )

            self.logger.info(f"Warm-up session flow for @{username}")
            self.api_manager.warmup_session(username)

            user_id = self._get_user_id(username)
            self.logger.success(f"Resolved @{username} -> user_id={user_id}")

            self.logger.info(f"Sequential Step A: fetching UserTweets for @{username}")
            tweets_only = self._fetch_endpoint_pages(
                account=username,
                user_id=user_id,
                endpoint="UserTweets",
                max_pages=max_pages,
            )
            self._persist_endpoint_output(username, "UserTweets", tweets_only)
            self.logger.success(f"@{username} UserTweets complete: {len(tweets_only)} item(s)")

            self.logger.info(f"Sequential Step B: fetching UserTweetsAndReplies for @{username}")
            tweets_and_replies = self._fetch_endpoint_pages(
                account=username,
                user_id=user_id,
                endpoint="UserTweetsAndReplies",
                max_pages=max_pages,
            )
            self._persist_endpoint_output(username, "UserTweetsAndReplies", tweets_and_replies)
            self.logger.success(
                f"@{username} UserTweetsAndReplies complete: {len(tweets_and_replies)} item(s)"
            )

            self.logger.success(f"Account @{username} fully completed; moving to next account")

        self.logger.success("All accounts completed.")


def main():
    engine = FetcherEngine(config_path="config/config.json")
    engine.run()


if __name__ == "__main__":
    main()
