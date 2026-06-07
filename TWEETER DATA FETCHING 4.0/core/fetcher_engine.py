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
from urllib.parse import quote, urlencode

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

    def _endpoint_payload_config(self, endpoint: str) -> Dict[str, Any]:
        payloads = self.config.get("graphql_endpoint_payloads", {})
        endpoint_payload = payloads.get(endpoint, {})
        return endpoint_payload if isinstance(endpoint_payload, dict) else {}

    def _timeline_features(self, endpoint: Optional[str] = None) -> Dict[str, Any]:
        if endpoint:
            configured = self._endpoint_payload_config(endpoint).get("features")
            if isinstance(configured, dict):
                return dict(configured)
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

    def _timeline_field_toggles(self, endpoint: str) -> Dict[str, Any]:
        configured = self._endpoint_payload_config(endpoint).get("fieldToggles")
        if isinstance(configured, dict):
            return dict(configured)
        return {"withArticlePlainText": False}

    def _timeline_variables(self, endpoint: str, user_id: str, cursor: Optional[str]) -> Dict[str, Any]:
        variables_config = self._endpoint_payload_config(endpoint).get("variables")
        if isinstance(variables_config, dict):
            template_key = "pagination" if cursor else "initial"
            template = variables_config.get(template_key)
            if isinstance(template, dict):
                variables = dict(template)
                variables["userId"] = user_id
                if cursor:
                    variables["cursor"] = cursor
                else:
                    variables.pop("cursor", None)
                return variables

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
        return variables

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
        return f"{base_url}?{urlencode(query_params, quote_via=quote)}"

    def _get_user_id(self, username: str) -> str:
        cached_user_id = self.storage_manager.get_user_id(username)
        if cached_user_id:
            return cached_user_id

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
        self.storage_manager.set_user_id(username, str(user_id))
        return str(user_id)

    def _fetch_endpoint_result(
        self,
        *,
        account: str,
        user_id: str,
        endpoint: str,
        max_pages: int,
        batch_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        started_at = datetime.utcnow().isoformat() + "Z"
        attempts = 0
        error_samples: List[Dict[str, Any]] = []
        last_http_status: Optional[int] = None

        def make_result(
            *,
            status: str,
            outcome: str,
            reason: str,
            pages: List[Dict[str, Any]],
            last_cursor: Optional[str],
            raw_batch: Path,
        ) -> Dict[str, Any]:
            return {
                "account": account,
                "endpoint": endpoint,
                "status": status,
                "outcome": outcome,
                "reason": reason,
                "pages": pages,
                "pages_fetched": len(pages),
                "raw_batch_path": str(raw_batch),
                "last_cursor": last_cursor,
                "last_http_status": last_http_status,
                "attempts": attempts,
                "error_samples": error_samples[-5:],
                "started_at": started_at,
                "finished_at": datetime.utcnow().isoformat() + "Z",
            }

        def record_http_error(response, cursor_value: Optional[str], attempt_number: int) -> None:
            nonlocal last_http_status
            last_http_status = int(response.status_code)
            sample = {
                "status_code": int(response.status_code),
                "cursor": cursor_value,
                "attempt": attempt_number,
                "response_text": str(response.text or "")[:500],
            }
            error_samples.append(sample)

        def classify_http_failure(status_code: int, has_pages: bool, cursor_value: Optional[str]) -> Tuple[str, str, str]:
            if status_code == 404 and cursor_value and has_pages:
                return "partial", "partial_cursor_404", "Cursor returned 404 after successful pages"
            if status_code == 404:
                return "failed", "failed_initial_404", "Initial page returned 404"
            if status_code in {401, 403}:
                if has_pages:
                    return "partial", "partial_http_error", f"HTTP {status_code} after successful pages"
                return "failed", "failed_initial_auth", f"Initial request returned HTTP {status_code}"
            if status_code == 429:
                if has_pages:
                    return "partial", "partial_rate_limited", "Rate limit persisted after successful pages"
                return "failed", "failed_initial_rate_limit", "Initial request stayed rate-limited"
            if 500 <= status_code < 600:
                if has_pages:
                    return "partial", "partial_http_error", f"HTTP {status_code} after successful pages"
                return "failed", "failed_initial_http_error", f"Initial request returned HTTP {status_code}"
            if has_pages:
                return "partial", "partial_http_error", f"HTTP {status_code} after successful pages"
            return "failed", "failed_initial_http_error", f"Initial request returned HTTP {status_code}"

        def finish_with_state(
            *,
            status: str,
            outcome: str,
            reason: str,
            pages: List[Dict[str, Any]],
            cursor_value: Optional[str],
            raw_batch: Path,
        ) -> Dict[str, Any]:
            state_status = "completed" if status == "completed" else status
            state_cursor = "__END__" if status == "completed" else (cursor_value if cursor_value else "__START__")
            self.storage_manager.update_endpoint_state(
                account,
                endpoint,
                last_cursor=state_cursor,
                status=state_status,
                meta={
                    "outcome": outcome,
                    "reason": reason,
                    "last_http_status": last_http_status,
                    "pages_fetched": len(pages),
                    "raw_batch_path": str(raw_batch),
                    "finished_at": datetime.utcnow().isoformat() + "Z",
                },
            )
            return make_result(
                status=status,
                outcome=outcome,
                reason=reason,
                pages=pages,
                last_cursor=state_cursor,
                raw_batch=raw_batch,
            )

        query_id = self.api_manager.get_query_id(endpoint)
        if not query_id:
            raise RuntimeError(f"Missing query ID for endpoint: {endpoint}")

        features = self._timeline_features(endpoint)
        field_toggles = self._timeline_field_toggles(endpoint)
        existing_state = self.storage_manager.get_endpoint_state(account, endpoint)
        status_value = str(existing_state.get("status", "pending"))
        resume_cursor = existing_state.get("last_cursor")
        raw_batch_path = existing_state.get("raw_batch_path")
        if batch_dir is None:
            if raw_batch_path and Path(str(raw_batch_path)).exists():
                batch_dir = Path(str(raw_batch_path))
            else:
                batch_dir = self.storage_manager.create_raw_batch_dir(endpoint, account)

        existing_pages = self.storage_manager.load_raw_pages_from_batch(batch_dir)
        cursor: Optional[str] = (
            str(resume_cursor)
            if (
                resume_cursor
                and status_value in {"running", "paused", "failed"}
                and str(resume_cursor) not in {"__START__", "__END__"}
            )
            else None
        )
        if cursor:
            self.logger.warning(
                f"Resuming @{account} {endpoint} from saved cursor: {cursor}"
            )
        if status_value == "completed" and existing_pages:
            self.logger.info(f"Skipping @{account} {endpoint}; completed batch exists at {batch_dir}")
            return make_result(
                status="completed",
                outcome=str(existing_state.get("outcome") or existing_state.get("reason") or "skipped_existing_completed"),
                reason="Completed raw batch already exists",
                pages=existing_pages,
                last_cursor=str(existing_state.get("last_cursor") or "__END__"),
                raw_batch=batch_dir,
            )

        self.api_manager.warmup_navigation_context(username=account, endpoint=endpoint)

        # Mark active run state at loop start.
        self.storage_manager.update_endpoint_state(
            account,
            endpoint,
            last_cursor=cursor if cursor else None,
            status="running",
            meta={"raw_batch_path": str(batch_dir)},
        )

        page = len(existing_pages) + 1
        all_items: List[Dict[str, Any]] = list(existing_pages)

        policy = self.api_manager.retry_policy()

        while page <= max_pages:
            variables = self._timeline_variables(endpoint, user_id, cursor)

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
            page_request_succeeded = False
            max_attempts = max(
                int(policy.get("client_error_attempts", self.max_cursor_error_retries)),
                int(policy.get("server_error_attempts", self.max_cursor_error_retries)),
                int(policy.get("request_error_attempts", self.max_cursor_error_retries)),
            )

            for attempt in range(max_attempts):
                attempts += 1
                try:
                    response = self.api_manager.perform_get(
                        endpoint=endpoint,
                        url=request_url,
                        max_retries=1,
                        username=account,
                        headers=request_headers,
                    )
                    last_http_status = int(response.status_code)

                    if response.status_code == 429:
                        record_http_error(response, cursor, attempt + 1)
                        self._log_4xx_details(
                            account=account,
                            endpoint=endpoint,
                            response=response,
                            request_url=request_url,
                            request_headers=dict(response.request.headers),
                            variables=variables,
                            cursor=cursor,
                            title="RATE LIMITED (SLEEPING AND RETRYING)",
                        )
                        wait = self.api_manager.rate_limit_sleep_seconds(endpoint, response.headers)
                        if wait <= 0:
                            wait = int(policy.get("rate_limit_safety_buffer_seconds", 5))
                        if attempt >= max_attempts - 1:
                            status, outcome, reason = classify_http_failure(429, bool(all_items), cursor)
                            return finish_with_state(
                                status=status,
                                outcome=outcome,
                                reason=reason,
                                pages=all_items,
                                cursor_value=cursor,
                                raw_batch=batch_dir,
                            )
                        self.logger.warning(
                            f"@{account} {endpoint} hit HTTP 429; retrying same page/cursor after {wait}s"
                        )
                        time.sleep(wait)
                        continue

                    if response.status_code in {400, 401, 403, 404}:
                        record_http_error(response, cursor, attempt + 1)
                        client_attempts = int(policy.get("client_error_attempts", self.max_cursor_error_retries))
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
                                if attempt < client_attempts - 1
                                else "CURSOR ERROR (MAX RETRIES REACHED)"
                            ),
                        )
                        if attempt < client_attempts - 1:
                            wait = self.api_manager.jitter_sleep(
                                float(policy.get("client_error_min_seconds", 10)),
                                float(policy.get("client_error_max_seconds", 20)),
                                reason=f"@{account} {endpoint} HTTP {response.status_code} retry {attempt + 1}/{client_attempts}",
                            )
                            self.logger.warning(
                                f"@{account} {endpoint} got HTTP {response.status_code}; retried after {wait:.1f}s "
                                f"(attempt {attempt + 1}/{client_attempts})"
                            )
                            continue

                        status, outcome, reason = classify_http_failure(
                            int(response.status_code), bool(all_items), cursor
                        )
                        self.logger.warning(
                            f"@{account} {endpoint} classified as {outcome}; moving to next account/endpoint."
                        )
                        return finish_with_state(
                            status=status,
                            outcome=outcome,
                            reason=reason,
                            pages=all_items,
                            cursor_value=cursor,
                            raw_batch=batch_dir,
                        )

                    if 500 <= response.status_code < 600:
                        record_http_error(response, cursor, attempt + 1)
                        server_attempts = int(policy.get("server_error_attempts", self.max_cursor_error_retries))
                        if attempt < server_attempts - 1:
                            base = float(policy.get("server_error_base_seconds", 5))
                            max_sleep = float(policy.get("server_error_max_seconds", 60))
                            wait = min(max_sleep, base * (2 ** attempt))
                            self.api_manager.jitter_sleep(wait, wait + base, reason=f"@{account} {endpoint} HTTP {response.status_code}")
                            continue
                        status, outcome, reason = classify_http_failure(int(response.status_code), bool(all_items), cursor)
                        return finish_with_state(
                            status=status,
                            outcome=outcome,
                            reason=reason,
                            pages=all_items,
                            cursor_value=cursor,
                            raw_batch=batch_dir,
                        )

                    response.raise_for_status()
                    page_request_succeeded = True
                    break
                except Exception as exc:
                    error_samples.append({
                        "cursor": cursor,
                        "attempt": attempt + 1,
                        "exception": str(exc)[:500],
                    })
                    request_attempts = int(policy.get("request_error_attempts", self.max_cursor_error_retries))
                    if attempt < request_attempts - 1:
                        base = float(policy.get("request_error_base_seconds", 5))
                        max_sleep = float(policy.get("request_error_max_seconds", 60))
                        wait = min(max_sleep, base * (2 ** attempt))
                        self.logger.warning(
                            f"@{account} {endpoint} request error: {exc}; retrying "
                            f"(attempt {attempt + 1}/{request_attempts})"
                        )
                        self.api_manager.jitter_sleep(wait, wait + base, reason=f"@{account} {endpoint} request error")
                        continue

                    status = "partial" if all_items else "failed"
                    outcome = "partial_request_error" if all_items else "failed_initial_request_error"
                    self.logger.warning(
                        f"@{account} {endpoint} classified as {outcome}; moving on."
                    )
                    return finish_with_state(
                        status=status,
                        outcome=outcome,
                        reason=str(exc)[:500],
                        pages=all_items,
                        cursor_value=cursor,
                        raw_batch=batch_dir,
                    )

            if not page_request_succeeded:
                status = "partial" if all_items else "failed"
                outcome = "partial_unknown_error" if all_items else "failed_initial_unknown_error"
                return finish_with_state(
                    status=status,
                    outcome=outcome,
                    reason="Request loop ended without a successful response",
                    pages=all_items,
                    cursor_value=cursor,
                    raw_batch=batch_dir,
                )

            if response is None:
                status = "partial" if all_items else "failed"
                outcome = "partial_empty_response" if all_items else "failed_initial_empty_response"
                return finish_with_state(
                    status=status,
                    outcome=outcome,
                    reason="No response object returned",
                    pages=all_items,
                    cursor_value=cursor,
                    raw_batch=batch_dir,
                )

            try:
                payload = response.json()
            except Exception as exc:
                error_samples.append({
                    "cursor": cursor,
                    "page": page,
                    "exception": f"JSON parse error: {str(exc)[:500]}",
                })
                status = "partial" if all_items else "failed"
                outcome = "partial_parse_error" if all_items else "failed_initial_parse_error"
                return finish_with_state(
                    status=status,
                    outcome=outcome,
                    reason=f"Could not parse JSON response: {str(exc)[:500]}",
                    pages=all_items,
                    cursor_value=cursor,
                    raw_batch=batch_dir,
                )

            all_items.append(payload)
            self.storage_manager.save_raw_page(batch_dir, page, payload)
            next_cursor = self._extract_bottom_cursor(payload)

            self.storage_manager.update_endpoint_state(
                account,
                endpoint,
                last_cursor=next_cursor if next_cursor else "__END__",
                status="running",
                meta={
                    "last_page_fetched_at": datetime.utcnow().isoformat() + "Z",
                    "last_page_number": page,
                    "raw_batch_path": str(batch_dir),
                },
            )

            self.logger.pagination(account=account, endpoint=endpoint, page=page, cursor=next_cursor)
            if next_cursor:
                self.logger.info(
                    f"Page {page} fetched -> Cursor found: {next_cursor} -> Requesting Page {page + 1}"
                )
                cursor = next_cursor
                page += 1
                self.api_manager.human_delay("between_pages")
                continue

            self.logger.info(
                f"Account: @{account} | Endpoint: {endpoint} | End of pagination reached"
            )
            return finish_with_state(
                status="completed",
                outcome="success_true_end",
                reason="End of pagination reached without cursor",
                pages=all_items,
                cursor_value="__END__",
                raw_batch=batch_dir,
            )

        if page > max_pages:
            return finish_with_state(
                status="completed",
                outcome="success_max_pages",
                reason="Configured max_pages reached",
                pages=all_items,
                cursor_value=cursor if cursor else "__END__",
                raw_batch=batch_dir,
            )

        return finish_with_state(
            status="completed",
            outcome="success_true_end",
            reason="Endpoint fetch completed",
            pages=all_items,
            cursor_value="__END__",
            raw_batch=batch_dir,
        )

    def _fetch_endpoint_pages(
        self,
        *,
        account: str,
        user_id: str,
        endpoint: str,
        max_pages: int,
        batch_dir: Optional[Path] = None,
    ) -> List[Dict[str, Any]]:
        return self._fetch_endpoint_result(
            account=account,
            user_id=user_id,
            endpoint=endpoint,
            max_pages=max_pages,
            batch_dir=batch_dir,
        ).get("pages", [])

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
