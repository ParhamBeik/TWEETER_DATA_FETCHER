#!/usr/bin/env python3
"""Fetch profile timeline pages from Twitter/X.

Run:
    python -m fetcher.timeline

For normal historical/live runs use ``fetcher.historical`` or ``fetcher.live``; this
module runs the lower-level sequential fetch engine with configured accounts.

Code map:
- EngineLogger keeps console output readable.
- FetcherEngine loads config, resolves user IDs, paginates endpoints, and saves raw pages.
- The bottom cursor is the only pagination value that may be reused.
- Storage and tweet-set processing happen in other modules.
"""
from __future__ import annotations


import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from fetcher.config import PROJECT_ROOT
from fetcher.client import APIManager
from fetcher.processing import (
    RollingWindowEvaluator,
    extract_bottom_cursor,
    timeline_field_toggles,
    timeline_variables,
    user_by_screen_name_contract,
    validate_graphql_payload,
)
from fetcher.browser import BrowserBootstrap, BrowserBootstrapResult
from fetcher.storage import StorageManager
from fetcher.config import load_tier_config
from zoneinfo import ZoneInfo

from fetcher.observability import EventRecorder, redact_exception
from fetcher.observability import configure_logging
from fetcher.observability import PipelineConsole


TIMEZONE = "Asia/Tehran"
DEFAULT_HISTORICAL_MAX_PAGES = 15

# A profile timeline that has run out of tweets does NOT stop handing out bottom
# cursors: past the last real tweet X keeps returning pages whose only entries
# are the two cursor entries. Without this stop the loop paginates into that void
# until the safety cap, reports "partial", and -- because a partial run may not
# advance the backfill watermark -- refetches the same account from page 1 on
# every single tick, forever. Observed in production: one account burning the
# whole shared UserTweets budget every 15 minutes and starving live polling to
# zero. Two consecutive tweet-less pages is the end of the timeline.
EMPTY_PAGE_STREAK = 2

# Outcomes that prove pagination reached the actual end of an account's
# timeline. `success_window_complete` is deliberately NOT one of them: it only
# means the caller's rolling window was satisfied, with a live cursor still on
# offer, so treating it as the end would let a deep archive walk declare an
# account fully collected after covering a few days of it.
TIMELINE_END_OUTCOMES = frozenset({"success_true_end", "success_timeline_exhausted"})


def _cursor_reference(cursor: Optional[str]) -> Optional[str]:
    """Return a stable diagnostic fingerprint without exposing the cursor."""
    if not cursor:
        return None
    return hashlib.sha256(cursor.encode("utf-8")).hexdigest()[:12]


def _response_latency_ms(response: Any, request_started: float) -> int:
    """Prefer transport time so rate-limit and pacing waits are not reported as latency."""
    seconds = getattr(response, "elapsed_seconds", None)
    if seconds is None:
        total_seconds = getattr(getattr(response, "elapsed", None), "total_seconds", None)
        seconds = total_seconds() if callable(total_seconds) else None
    if seconds is None:
        seconds = time.monotonic() - request_started
    return max(0, int(float(seconds) * 1000))


# Backward-compatible alias
EngineLogger = PipelineConsole


# Timeline fetching ---------------------------------------------------------


class FetcherEngine:
    """Phase 2 sequential fetcher with strict failure visibility."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        subsystem: str = "historical",
    ):
        self.project_root = PROJECT_ROOT
        raw_subsystem = str(subsystem or "historical").strip().lower()
        self.subsystem = "historical_live" if raw_subsystem in {"historical", "live"} else raw_subsystem
        self.data_root = self.project_root / "data"
        console_subsystem = "live" if self.subsystem == "historical_live" and raw_subsystem == "live" else (
            "historical" if self.subsystem == "historical_live" else self.subsystem
        )
        self.logger = PipelineConsole(console_subsystem)
        logs_dir = self.data_root / self.subsystem / "logs"
        self.recorder = EventRecorder(logs_dir, subsystem=self.subsystem)
        configure_logging(subsystem=self.subsystem, logs_dir=logs_dir)
        self.api_manager = APIManager(
            config_path=config_path,
            state_dir=self.data_root / self.subsystem / "state",
            console=self.logger,
            recorder=self.recorder,
        )
        self.storage_manager = StorageManager(
            base_dir=self.project_root,
            timezone=TIMEZONE,
            subsystem=self.subsystem,
            data_root_override=self.data_root,
        )
        self.window_evaluator = RollingWindowEvaluator()

        self.config = self.api_manager.config
        self.tz = ZoneInfo(TIMEZONE)
        self.account_map, self.priority_policies = load_tier_config(self.config)
        self.max_cursor_error_retries = int(
            self.config.get("api_config", {}).get("cursor_error_max_retries", 3)
        )
        self.first_request_warmup_seconds = int(
            self.config.get("api_config", {}).get("first_request_warmup_seconds", 0)
        )
        self.pagination_safety_cap_pages = int(
            self.config.get("api_config", {}).get("pagination_safety_cap_pages", 50)
        )
        self.backoff_schedule_seconds = [15, 30, 60]
        self.max_404_recoveries = int(
            self.config.get("api_config", {}).get("pagination_404_recovery_attempts", 1)
        )
        browser_cfg = self.config.get("browser_bootstrap", {}) or {}
        self.browser_bootstrap = BrowserBootstrap(
            self.config,
            headless=bool(browser_cfg.get("headless", True)),
            timeout_ms=int(browser_cfg.get("timeout_ms", 60000)),
        )
        self.last_bootstrap: Optional[BrowserBootstrapResult] = None

        self.logger.show_startup_config(
            self.config,
            self.account_map,
            self.priority_policies,
            str(self.api_manager.config_path),
        )

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
            "post_ctas_fetch_enabled": False,
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

    def _timeline_field_toggles(self, endpoint: str) -> Optional[Dict[str, Any]]:
        configured = self._endpoint_payload_config(endpoint).get("fieldToggles")
        if isinstance(configured, dict):
            return dict(configured) if configured else None
        return timeline_field_toggles(endpoint)

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

        return timeline_variables(endpoint, user_id, cursor)

    def _extract_bottom_cursor(self, payload: Dict[str, Any]) -> Optional[str]:
        return extract_bottom_cursor(payload)

    def bootstrap_browser_context(
        self,
        *,
        username: Optional[str] = None,
        search_url: Optional[str] = None,
        capture_endpoint: Optional[str] = None,
        max_pages: int = 2,
        stop_when: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> BrowserBootstrapResult:
        result = self.browser_bootstrap.run(
            username=username,
            search_url=search_url,
            capture_endpoint=capture_endpoint,
            max_pages=max_pages,
            stop_when=stop_when,
        )
        self.last_bootstrap = result
        if result.ok:
            self.api_manager.apply_browser_context(
                cookies=result.cookies,
                query_ids=result.query_ids,
                request_headers=result.request_headers,
            )
        else:
            self.logger.warning(f"Browser bootstrap unavailable/failed: {result.error}")
        return result

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
        """Record a resumable 4xx without leaking request context to stdout."""
        detail_ref = None
        try:
            detail_ref = self.recorder.emit_http_error(
                account=account,
                endpoint=endpoint,
                status_code=int(response.status_code),
                cursor=cursor,
                request_url=request_url,
                request_headers=request_headers,
                variables=variables,
                response_text=response.text or "",
            )
        except Exception as exc:  # pragma: no cover - best-effort event recording
            self.logger.warning(f"Failed to record http error event: {exc}")
        self.logger.error_one_liner(
            f"HTTP {response.status_code} account=@{account} endpoint={endpoint} action=classify",
            detail_ref=detail_ref,
        )

    def _recover_404_context(
        self,
        *,
        account: str,
        endpoint: str,
        cursor: Optional[str],
        batch_dir: Path,
        pages_fetched: int,
    ) -> bool:
        self.storage_manager.update_endpoint_state(
            account,
            endpoint,
            last_cursor=cursor if cursor else "__START__",
            status="running",
            meta={
                "raw_batch_path": str(batch_dir),
                "pages_fetched": pages_fetched,
                "recovery_started_at": datetime.utcnow().isoformat() + "Z",
                "recovery_reason": "pagination_404_context_rejected",
            },
        )
        # Automatic auth recovery via a headless sniffer has been retired
        # (YAGNI: it was the least-reliable part of the auth path). The v4
        # sniffer is now a pure diagnostic tool. Refresh auth/query-ids manually
        # via fetcher/twitter/auth.py.
        self.logger.warning(
            f"@{account} {endpoint} 404/context-rejected; automatic auth recovery is disabled "
            "(run auto_refresh.py --interactive manually to refresh)."
        )
        return False

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

    def build_user_by_screen_name_url(
        self,
        username: str,
        query_id: str,
    ) -> Tuple[str, Dict[str, Any]]:
        """Build the browser-verified UserByScreenName request contract."""
        request_contract = user_by_screen_name_contract(username)
        request_url = self._build_graphql_url(
            endpoint="UserByScreenName",
            query_id=query_id,
            variables=request_contract["variables"],
            features=request_contract["features"],
            field_toggles=request_contract["fieldToggles"],
        )
        return request_url, request_contract

    def _get_user_id(self, username: str) -> str:
        cached_user_id = self.storage_manager.get_user_id(username)
        if cached_user_id:
            return cached_user_id

        query_id = self.api_manager.get_query_id("UserByScreenName")
        if not query_id:
            raise RuntimeError("Missing query ID for UserByScreenName")

        endpoint = "UserByScreenName"
        request_url, request_contract = self.build_user_by_screen_name_url(username, query_id)
        response = self.api_manager.perform_get(endpoint=endpoint, url=request_url, username=username)

        if response.status_code in {400, 401, 403, 404}:
            self._log_4xx_details(
                account=username,
                endpoint=endpoint,
                response=response,
                request_url=request_url,
                request_headers=dict(response.request.headers),
                variables=request_contract["variables"],
                cursor=None,
                title="USER LOOKUP 4xx ERROR",
            )
            raise RuntimeError(f"{endpoint} returned HTTP {response.status_code}")

        response.raise_for_status()
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"{endpoint} JSON parse failure: {str(exc)[:300]}") from exc
        validation = validate_graphql_payload(endpoint, payload)
        if not validation.ok:
            raise RuntimeError(f"{endpoint} semantic failure: {validation.reason}")
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
        max_pages: Optional[int] = None,
        window_days: Optional[int] = None,
        cutoff: Optional[datetime] = None,
        batch_dir: Optional[Path] = None,
        force_refetch: bool = False,
        min_remaining: int = 0,
        resume_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        started_at = datetime.utcnow().isoformat() + "Z"
        attempts = 0
        empty_page_streak = 0
        error_samples: List[Dict[str, Any]] = []
        last_http_status: Optional[int] = None
        latest_window_coverage: Optional[Dict[str, Any]] = None
        transport = "http"
        cursor_termination_reason: Optional[str] = None
        page_output_paths: List[str] = []

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
                "cursor_termination_reason": cursor_termination_reason,
                "last_http_status": last_http_status,
                "attempts": attempts,
                "error_samples": error_samples[-5:],
                "rate_headers": self.api_manager.rate_limits.get(endpoint, {}),
                "started_at": started_at,
                "finished_at": datetime.utcnow().isoformat() + "Z",
                "window_coverage": latest_window_coverage,
                "transport": transport,
                "bootstrap_route": self.last_bootstrap.route if self.last_bootstrap else None,
                "output_paths": {
                    "raw_batch": str(raw_batch),
                    "pages": list(page_output_paths),
                },
            }

        def record_http_error(response, cursor_value: Optional[str], attempt_number: int) -> None:
            nonlocal last_http_status
            last_http_status = int(response.status_code)
            sample = {
                "status_code": int(response.status_code),
                "cursor_ref": _cursor_reference(cursor_value),
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
            completion_meta = {
                "outcome": outcome,
                "reason": reason,
                "last_http_status": last_http_status,
                "pages_fetched": len(pages),
                "transport": transport,
                "cursor_termination_reason": cursor_termination_reason,
                "raw_batch_path": str(raw_batch),
                "finished_at": datetime.utcnow().isoformat() + "Z",
            }
            # Advance the rolling-window watermark ONLY on a successful completion, using
            # the run's start time. A partial/failed run leaves the old watermark intact
            # so the next run re-covers the gap (backfill goes beyond the last fetch).
            # A resumed archive tick is excluded: it started thousands of tweets deep in
            # the past and never saw the top of the timeline, so it cannot claim to have
            # fetched everything up to `started_at`.
            if state_status == "completed" and not archive_resume:
                completion_meta["fetch_watermark"] = started_at
            self.storage_manager.update_endpoint_state(
                account,
                endpoint,
                last_cursor=state_cursor,
                status=state_status,
                meta=completion_meta,
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
        safety_cap = max(1, int(max_pages or self.pagination_safety_cap_pages))

        features = self._timeline_features(endpoint)
        field_toggles = self._timeline_field_toggles(endpoint)
        existing_state = self.storage_manager.get_endpoint_state(account, endpoint)
        status_value = str(existing_state.get("status", "pending"))
        saved_cursor = existing_state.get("last_cursor")
        raw_batch_path = existing_state.get("raw_batch_path")
        # An explicit resume_cursor is the deep archive walk handing back its own
        # position from a previous tick. It must win over `last_cursor`, which the
        # shallow live poll writes to this same (account, endpoint) row: the two
        # walks sit at completely different depths in the timeline and inheriting
        # each other's cursor would either restart the archive at the top or send
        # the live poll thousands of tweets into the past.
        archive_resume = bool(resume_cursor and str(resume_cursor) not in {"__START__", "__END__"})
        if archive_resume or force_refetch:
            batch_dir = self.storage_manager.create_raw_batch_dir(endpoint, account)
        elif batch_dir is None:
            if raw_batch_path and Path(str(raw_batch_path)).exists():
                batch_dir = Path(str(raw_batch_path))
            else:
                batch_dir = self.storage_manager.create_raw_batch_dir(endpoint, account)

        # Each archive tick is its own batch, so its page numbering restarts and
        # `safety_cap` acts as this tick's page budget rather than a lifetime cap.
        existing_pages = (
            [] if (archive_resume or force_refetch)
            else self.storage_manager.load_raw_pages_from_batch(batch_dir)
        )
        if archive_resume:
            cursor: Optional[str] = str(resume_cursor)
            status_value = "running"
        elif force_refetch:
            cursor = None
            status_value = "pending"
        else:
            cursor = (
                str(saved_cursor)
                if (
                    saved_cursor
                    and status_value in {"running", "paused", "failed"}
                    and str(saved_cursor) not in {"__START__", "__END__"}
                )
                else None
            )
        if cursor:
            self.logger.warning(
                f"Resuming @{account} {endpoint} from saved cursor: {cursor}"
            )
        if status_value == "completed" and existing_pages and window_days:
            self.logger.info(
                f"@{account} {endpoint} has completed stale pages, but current-cycle freshness is required; refetching initial page."
            )
            batch_dir = self.storage_manager.create_raw_batch_dir(endpoint, account)
            cursor = None
            page = 1
            all_items = []
        else:
            page = len(existing_pages) + 1
            all_items = list(existing_pages)

        self.api_manager.warmup_navigation_context(username=account, endpoint=endpoint)
        if self.first_request_warmup_seconds > 0 and not existing_pages:
            self.logger.info(
                f"Mandatory first-request warm-up for @{account} {endpoint}: "
                f"{self.first_request_warmup_seconds}s"
            )
            time.sleep(self.first_request_warmup_seconds)

        # Mark active run state at loop start.
        self.storage_manager.update_endpoint_state(
            account,
            endpoint,
            last_cursor=cursor if cursor else None,
            status="running",
            meta={"raw_batch_path": str(batch_dir)},
        )

        policy = self.api_manager.retry_policy()
        recovery_counts: Dict[str, int] = {}
        context_refreshed = False

        while page <= safety_cap:
            # Historical and live spend one shared endpoint budget. The deep walk
            # stops at a floor so the live poller always has requests left; before
            # this, one backfill drained the bucket every tick and live deferred
            # every account it had. Stopping here keeps the cursor, so the walk
            # resumes from this exact page next tick instead of restarting.
            if min_remaining and self.api_manager.remaining_requests(endpoint, min_remaining) <= 0:
                cursor_termination_reason = "quota_floor_reached"
                self.logger.warning(
                    f"@{account} {endpoint} paused at page {page}: budget down to the "
                    f"{min_remaining}-request floor reserved for live polling"
                )
                return finish_with_state(
                    status="partial",
                    outcome="paused_for_quota",
                    reason=f"{endpoint} budget reached the {min_remaining}-request reserve floor",
                    pages=all_items,
                    cursor_value=cursor,
                    raw_batch=batch_dir,
                )
            page_started = time.monotonic()
            cursor_termination_reason = None
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
            ) + max(0, self.max_404_recoveries)

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
                        if response.status_code == 400:
                            cursor_termination_reason = "bad_request_contract"
                            status, outcome, reason = classify_http_failure(400, bool(all_items), cursor)
                            return finish_with_state(status=status, outcome=outcome, reason=reason, pages=all_items, cursor_value=cursor, raw_batch=batch_dir)
                        if response.status_code in {401, 403}:
                            cursor_termination_reason = "auth_required"
                            return finish_with_state(
                                status="partial" if all_items else "failed",
                                outcome="auth_required",
                                reason="X authentication expired; refresh the operator session manually",
                                pages=all_items,
                                cursor_value=cursor,
                                raw_batch=batch_dir,
                            )
                        if response.status_code == 404 and not cursor and not all_items and not context_refreshed:
                            context_refreshed = True
                            refreshed = self.bootstrap_browser_context(username=account, max_pages=1)
                            if refreshed.ok:
                                query_id = self.api_manager.get_query_id(endpoint) or query_id
                                request_url = self._build_graphql_url(
                                    endpoint=endpoint,
                                    query_id=query_id,
                                    variables=variables,
                                    features=features,
                                    field_toggles=field_toggles,
                                )
                                continue
                        if response.status_code != 400 and attempt < client_attempts - 1:
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

                        cursor_key = cursor or "__START__"
                        if response.status_code == 404 and not cursor and not all_items:
                            fallback = self.bootstrap_browser_context(
                                username=account,
                                capture_endpoint=endpoint,
                                max_pages=min(2, safety_cap),
                            )
                            fallback_pages = fallback.target_pages.get(endpoint, []) if fallback.ok else []
                            valid_pages = [
                                page_payload for page_payload in fallback_pages
                                if validate_graphql_payload(endpoint, page_payload).ok
                            ]
                            if valid_pages:
                                transport = "browser_fallback"
                                for page_number, page_payload in enumerate(valid_pages, start=1):
                                    output_path = self.storage_manager.save_raw_page(batch_dir, page_number, page_payload)
                                    page_output_paths.append(str(output_path))
                                last_cursor = extract_bottom_cursor(valid_pages[-1])
                                cursor_termination_reason = "browser_fallback_initial_404"
                                # Profile endpoints must stay on HTTP for efficiency; browser
                                # recovery is evidence of a failed primary path, not success.
                                return finish_with_state(
                                    status="partial",
                                    outcome="partial_browser_fallback",
                                    reason="HTTP failed; browser recovered partial pages (not an efficient success path)",
                                    pages=valid_pages,
                                    cursor_value=last_cursor or "__END__",
                                    raw_batch=batch_dir,
                                )
                        if (
                            response.status_code == 404
                            and recovery_counts.get(cursor_key, 0) < self.max_404_recoveries
                        ):
                            recovery_counts[cursor_key] = recovery_counts.get(cursor_key, 0) + 1
                            self.logger.warning(
                                f"@{account} {endpoint} repeated HTTP 404 at "
                                f"cursor_ref={_cursor_reference(cursor)}; "
                                "saving cursor and refreshing browser parameters"
                            )
                            if self._recover_404_context(
                                account=account,
                                endpoint=endpoint,
                                cursor=cursor,
                                batch_dir=batch_dir,
                                pages_fetched=len(all_items),
                            ):
                                query_id = self.api_manager.get_query_id(endpoint) or query_id
                                request_url = self._build_graphql_url(
                                    endpoint=endpoint,
                                    query_id=query_id,
                                    variables=variables,
                                    features=features,
                                    field_toggles=field_toggles,
                                )
                                continue

                        status, outcome, reason = classify_http_failure(
                            int(response.status_code), bool(all_items), cursor
                        )
                        cursor_termination_reason = outcome
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
                        "cursor_ref": _cursor_reference(cursor),
                        "attempt": attempt + 1,
                        "exception": redact_exception(exc),
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
                    "cursor_ref": _cursor_reference(cursor),
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

            validation = validate_graphql_payload(endpoint, payload)
            if not validation.ok:
                error_samples.append({
                    "cursor_ref": _cursor_reference(cursor),
                    "page": page,
                    "semantic_error": validation.reason,
                    "graphql_errors": validation.errors[:3],
                })
                return finish_with_state(
                    status="partial" if all_items else "failed",
                    outcome="partial_graphql_error" if all_items else "failed_initial_graphql_error",
                    reason=validation.reason,
                    pages=all_items,
                    cursor_value=cursor,
                    raw_batch=batch_dir,
                )

            all_items.append(payload)
            output_path = self.storage_manager.save_raw_page(batch_dir, page, payload)
            page_output_paths.append(str(output_path))
            next_cursor = self._extract_bottom_cursor(payload)
            # Structured page-fetched event for the JSONL event stream.
            try:
                instructions = payload["data"]["user"]["result"]["timeline"]["timeline"]["instructions"]
                page_items = sum(len(instr.get("entries", [])) for instr in instructions)
            except Exception:
                page_items = 0
            latency_ms = _response_latency_ms(response, page_started)
            self.recorder.emit_page_fetched(
                account=account,
                endpoint=endpoint,
                page=page,
                cursor_in=cursor,
                cursor_out=next_cursor,
                http_status=int(response.status_code),
                items=page_items,
                transport=transport,
                latency_ms=latency_ms,
                attempt=attempt + 1,
                recovery="retried" if attempt else None,
            )
            if attempt:
                self.recorder.mark_http_recovered(account, endpoint)
            self.logger.page_row(
                account=account,
                endpoint=endpoint,
                page=page,
                transport=transport,
                items=page_items,
                cursor_status="found" if next_cursor else "end",
                http_status=int(response.status_code),
                latency_ms=latency_ms,
                attempt=attempt + 1,
                recovery="retried" if attempt else None,
                next_page=page + 1 if next_cursor else None,
            )
            if cutoff is not None:
                coverage = self.window_evaluator.evaluate_raw_pages_cutoff(
                    all_items, account, endpoint, cutoff
                )
            elif window_days:
                coverage = self.window_evaluator.evaluate_raw_pages(
                    all_items, account, endpoint, window_days
                )
            else:
                coverage = None
            latest_window_coverage = coverage.__dict__ if coverage else None

            self.storage_manager.update_endpoint_state(
                account,
                endpoint,
                last_cursor=next_cursor if next_cursor else "__END__",
                status="running",
                meta={
                    "last_page_fetched_at": datetime.utcnow().isoformat() + "Z",
                    "last_page_number": page,
                    "raw_batch_path": str(batch_dir),
                    "window_coverage": latest_window_coverage,
                },
            )

            # Count this page's own tweets, not the raw entry count: past the end
            # of a timeline X keeps serving pages whose only entries are the two
            # cursor entries, which is why an entry-count check would never fire.
            if self.window_evaluator.extract_endpoint_tweets([payload], account, endpoint):
                empty_page_streak = 0
            else:
                empty_page_streak += 1

            if coverage and coverage.complete:
                cursor_termination_reason = "current_chain_window_crossed"
                self.logger.info(
                    f"@{account} {endpoint} rolling window complete: "
                    f"oldest={coverage.oldest_date} targets={coverage.target_dates}"
                )
                return finish_with_state(
                    status="completed",
                    outcome="success_window_complete",
                    reason=f"Rolling window complete: {coverage.reason}",
                    pages=all_items,
                    cursor_value=next_cursor if next_cursor else "__END__",
                    raw_batch=batch_dir,
                )

            if empty_page_streak >= EMPTY_PAGE_STREAK:
                cursor_termination_reason = "timeline_exhausted"
                self.logger.info(
                    f"@{account} {endpoint} timeline exhausted: {empty_page_streak} "
                    f"consecutive pages with no tweets (cursor still offered)"
                )
                return finish_with_state(
                    status="completed",
                    outcome="success_timeline_exhausted",
                    reason=f"{empty_page_streak} consecutive pages returned no tweets",
                    pages=all_items,
                    cursor_value="__END__",
                    raw_batch=batch_dir,
                )

            if next_cursor:
                cursor = next_cursor
                page += 1
                self.api_manager.human_delay("between_pages")
                continue

            self.logger.info(
                f"Account: @{account} | Endpoint: {endpoint} | End of pagination reached"
            )
            cursor_termination_reason = "no_bottom_cursor"
            return finish_with_state(
                status="completed",
                outcome="success_true_end",
                reason="End of pagination reached without cursor",
                pages=all_items,
                cursor_value="__END__",
                raw_batch=batch_dir,
            )

        if page > safety_cap:
            cursor_termination_reason = "safety_cap_reached"
            return finish_with_state(
                status="partial",
                outcome="partial_safety_cap_reached",
                reason="Emergency safety page cap reached before rolling window completed",
                pages=all_items,
                cursor_value=cursor if cursor else "__END__",
                raw_batch=batch_dir,
            )

        cursor_termination_reason = "endpoint_loop_completed"
        return finish_with_state(
            status="completed",
            outcome="success_true_end",
            reason="Endpoint fetch completed",
            pages=all_items,
            cursor_value="__END__",
            raw_batch=batch_dir,
        )

TimelineFetcher = FetcherEngine
