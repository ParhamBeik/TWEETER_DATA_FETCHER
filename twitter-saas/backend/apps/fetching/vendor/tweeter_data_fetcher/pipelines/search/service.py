#!/usr/bin/env python3
"""SearchTimeline query monitor.

Runs configured search queries (from ``config/searches.json``), fetches pages per
product, parses tweets/cursors, and writes raw + processed exports under
``data/search/``.

Run:
    tdf-search --once
    python -m tweeter_data_fetcher.pipelines.search.service --once

Flags:
    --config <path>            config.json to use (else canonical config/)
    --search-config <path>     searches.json to use (else canonical config/)
    --only <name>              limit to named search(es) (repeatable)
    --once                     run a single poll cycle then exit
    --check-interval <sec>     seconds between cycles in continuous mode (default 60)
    --validation-run-id <id>   isolate output under data/validation/<id>/
"""
from __future__ import annotations


import argparse
import json
import logging
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urlencode

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
except Exception:
    Console = None
    Panel = None
    Table = None


# src/ must be importable when this module is run directly as a script.
_SRC_ROOT = Path(__file__).resolve().parents[3]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from tweeter_data_fetcher.paths import PROJECT_ROOT

from tweeter_data_fetcher.configuration import DEFAULT_PRIORITY_POLICIES
from tweeter_data_fetcher.x_api.timeline import FetcherEngine
from tweeter_data_fetcher.x_api.contracts import (
    SEARCH_TIMELINE_FEATURES,
    extract_bottom_cursor,
    search_timeline_variables,
    validate_graphql_payload,
)
from tweeter_data_fetcher.processing.sets import TweetSetProcessor
from tweeter_data_fetcher.processing.parsing import parse_twitter_timestamp
from tweeter_data_fetcher.storage.facade import StorageManager
from tweeter_data_fetcher.observability.pipeline_console import PipelineConsole
from tweeter_data_fetcher.observability.logging_setup import attach_run_id
from tweeter_data_fetcher.configuration import resolve_config_path


VALID_PRODUCTS = {"Top", "Latest", "Media", "People"}

FROZEN_SEARCH_FEATURES: Dict[str, object] = dict(SEARCH_TIMELINE_FEATURES)
logger = logging.getLogger(__name__)


def _agent_debug_log(hypothesis: str, location: str, event: str, fields: Dict[str, Any]) -> None:
    logger.debug("%s %s %s %s", hypothesis, location, event, fields)


# Search query building ------------------------------------------------------


class SearchQueryBuilder:
    """Build SearchTimeline rawQuery and browser URL from search definitions."""

    @staticmethod
    def _sanitize_term(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    @staticmethod
    def _ensure_handle(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "", str(value or "").strip().lstrip("@"))

    @staticmethod
    def _quote_phrase(value: str) -> str:
        text = SearchQueryBuilder._sanitize_term(value).replace('"', "")
        return f"\"{text}\"" if text else ""

    @staticmethod
    def normalize_product(value: str) -> str:
        candidate = str(value or "Top").strip().title()
        return candidate if candidate in VALID_PRODUCTS else "Top"

    @staticmethod
    def build_raw_query(search_def: Dict[str, Any], now_dt: datetime) -> str:
        if search_def.get("raw_query"):
            return str(search_def["raw_query"]).strip()
        if bool(search_def.get("preserve_exact_query", False)):
            explicit = str(search_def.get("exact_query") or "").strip()
            if explicit:
                return explicit

        parts: List[str] = []
        include_keywords = [SearchQueryBuilder._sanitize_term(term) for term in search_def.get("include_keywords", []) if SearchQueryBuilder._sanitize_term(term)]
        if include_keywords:
            parts.append(include_keywords[0] if len(include_keywords) == 1 else "(" + " OR ".join(include_keywords) + ")")
        for phrase in search_def.get("exact_phrases", []):
            clean = SearchQueryBuilder._sanitize_term(phrase).replace('"', "")
            if clean:
                parts.append(clean)
        for keyword in search_def.get("exclude_keywords", []):
            clean = SearchQueryBuilder._sanitize_term(keyword)
            if clean:
                parts.append(f"-{SearchQueryBuilder._quote_phrase(clean)}" if " " in clean else f"-{clean}")
        for key, prefix in [("from_accounts", "from:"), ("to_accounts", "to:")]:
            for account in search_def.get(key, []):
                handle = SearchQueryBuilder._ensure_handle(account)
                if handle:
                    parts.append(f"{prefix}{handle}")
        for mention in search_def.get("mentions", []):
            handle = SearchQueryBuilder._ensure_handle(mention)
            if handle:
                parts.append(f"@{handle}")
        for numeric_key in ["min_replies", "min_faves", "min_retweets"]:
            value = search_def.get(numeric_key)
            if value is not None and str(value).strip():
                parts.append(f"{numeric_key}:{int(value)}")
        lang = SearchQueryBuilder._sanitize_term(str(search_def.get("lang", "")))
        if lang:
            parts.append(f"lang:{lang}")

        since = SearchQueryBuilder._sanitize_term(str(search_def.get("since", "")))
        until = SearchQueryBuilder._sanitize_term(str(search_def.get("until", "")))
        since_days = search_def.get("since_days")
        if not since and since_days is not None:
            since = (now_dt - timedelta(days=int(since_days))).date().isoformat()
        if not until and since_days is not None and not bool(search_def.get("preserve_exact_query", False)):
            until = now_dt.date().isoformat()
        if since:
            parts.append(f"since:{since}")
        if until:
            parts.append(f"until:{until}")
        for extra in search_def.get("extra_filters", []):
            clean = SearchQueryBuilder._sanitize_term(extra)
            if clean:
                parts.append(clean)
        return " ".join(parts).strip()

    @staticmethod
    def build_human_search_url(raw_query: str, product: str) -> str:
        encoded_query = quote(raw_query, safe="()")
        filter_map = {"Top": "top", "Latest": "live", "Media": "media", "People": "user"}
        normalized = SearchQueryBuilder.normalize_product(product)
        return f"https://x.com/search?q={encoded_query}&f={filter_map.get(normalized, 'top')}&src=typed_query"

    @staticmethod
    def slug(search_def: Dict[str, Any]) -> str:
        raw = str(search_def.get("slug") or search_def.get("name") or "search_timeline")
        return re.sub(r"[^A-Za-z0-9_\\-]+", "_", raw).strip("_").lower() or "search_timeline"


# Search fetching and saving -------------------------------------------------


class SearchTimelineMonitor:
    """Continuous SearchTimeline monitor using v4 auth/rate-limit infrastructure."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        search_config_path: Optional[str] = None,
        validation_run_id: Optional[str] = None,
    ):
        self.project_root = PROJECT_ROOT
        self.validation_run_id = validation_run_id
        self.fetcher = FetcherEngine(config_path=config_path, subsystem="search", validation_run_id=validation_run_id)
        self.run_id = validation_run_id or self.fetcher.storage_manager.create_run_id()
        attach_run_id(self.run_id)
        self.fetcher.recorder.run_id = self.run_id
        self.api_manager = self.fetcher.api_manager
        self.config = self.api_manager.config
        self.storage = StorageManager(
            base_dir=self.project_root,
            subsystem="search",
            create_folders=False,
            manage_sync_state=False,
            data_root_override=self.fetcher.data_root,
        )
        self.processor = TweetSetProcessor()
        self.search_defs = self._load_search_config(search_config_path)
        self.search_root = self.fetcher.data_root / "search"
        self.raw_root = self.search_root / "raw"
        self.processed_root = self.search_root / "processed"
        self.debug_root = self.search_root / "debug"
        self.reports_root = self.search_root / "reports"
        self.state_file = self.search_root / "state" / "search_state.json"
        self.search_state = self._load_json(self.state_file, {})
        self.console = PipelineConsole(subsystem="search", verbosity="normal")
        for path in [self.raw_root, self.processed_root, self.debug_root, self.reports_root, self.state_file.parent]:
            path.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, path: str) -> Path:
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        return self.project_root / candidate

    def _load_search_config(self, path: Optional[str]) -> List[Dict[str, Any]]:
        cfg_path = resolve_config_path(path, project_root=self.project_root, filename="searches.json")
        with cfg_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("searches", [])
        return [row for row in data if isinstance(row, dict)]

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, type(default)) else default
            except Exception:
                return default
        return default

    @staticmethod
    def _save_json(path: Path, payload: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path

    def _policy_for_search(self, search_def: Dict[str, Any]) -> Dict[str, Any]:
        priority = int(search_def.get("polling_priority", 3))
        defaults = DEFAULT_PRIORITY_POLICIES.get(priority, DEFAULT_PRIORITY_POLICIES[7])
        default_cap = int(self.config.get("api_config", {}).get("pagination_safety_cap_pages", 50))
        if search_def.get("pagination_safety_cap_pages") is not None:
            page_cap = int(search_def["pagination_safety_cap_pages"])
        elif search_def.get("pagination_depth") is not None:
            page_cap = int(search_def["pagination_depth"])
        else:
            page_cap = default_cap
        return {
            "poll_interval_seconds": int(search_def.get("poll_interval_seconds", defaults["poll_interval_seconds"])),
            "pagination_safety_cap_pages": max(1, page_cap),
            "max_retries": int(search_def.get("max_retries", 3)),
            "rolling_hours": int(search_def.get("rolling_hours", 24)),
        }

    def _after_bootstrap(self, bootstrap: Any, endpoint: str = "SearchTimeline") -> None:
        if bootstrap.ok:
            self.api_manager.reconcile_bootstrap_params(
                endpoint,
                query_ids=bootstrap.query_ids,
                request_headers=bootstrap.request_headers,
            )

    @staticmethod
    def _compact_json(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    def _build_base_variables(self, search_def: Dict[str, Any], raw_query: str, product: str) -> Dict[str, Any]:
        return search_timeline_variables(
            raw_query=raw_query,
            product=product,
            count=int(search_def.get("count", 20)),
            query_source=str(search_def.get("query_source", "typed_query")),
            with_grok_translated_bio=bool(search_def.get("with_grok_translated_bio", True)),
            with_quick_promote_eligibility_tweet_fields=bool(search_def.get("with_quick_promote_eligibility_tweet_fields", False)),
        )

    def _build_frozen_headers(self, search_url: str) -> Dict[str, str]:
        headers = dict(self.api_manager.session.headers)
        headers["referer"] = search_url
        headers["x-twitter-active-user"] = "yes"
        return {str(key): str(value) for key, value in headers.items() if value is not None}

    def _extract_instructions(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        return (
            payload.get("data", {})
            .get("search_by_raw_query", {})
            .get("search_timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )

    def _collect_cursor_candidates(self, entry: Dict[str, Any], source_path: str) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        content = entry.get("content", {}) if isinstance(entry, dict) else {}
        if not isinstance(content, dict):
            return candidates
        entry_id = str(entry.get("entryId", ""))
        value = content.get("value")
        if value:
            cursor_type = str(content.get("cursorType", ""))
            is_bottom = cursor_type.lower() == "bottom" or entry_id.startswith("cursor-bottom-")
            candidates.append({
                "value": str(value),
                "source_path": source_path,
                "entry_id": entry_id,
                "typename": str(content.get("__typename", "")),
                "cursor_type": cursor_type,
                "is_bottom": is_bottom,
                "score": 100 if is_bottom else (70 if "cursor" in entry_id.lower() else 40),
            })
        for idx, item_entry in enumerate(content.get("items", []) if isinstance(content.get("items"), list) else []):
            nested = item_entry.get("item", {}).get("content", {}) if isinstance(item_entry, dict) else {}
            if isinstance(nested, dict) and nested.get("value"):
                is_bottom = str(nested.get("cursorType", "")).lower() == "bottom"
                candidates.append({
                    "value": str(nested["value"]),
                    "source_path": f"{source_path}.items[{idx}].item.content",
                    "entry_id": entry_id,
                    "typename": str(nested.get("__typename", "")),
                    "cursor_type": str(nested.get("cursorType", "")),
                    "is_bottom": is_bottom,
                    "score": 95 if is_bottom else 35,
                })
        return candidates

    def _parse_tweet_wrapper(self, wrapper: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        tweet_obj = wrapper.get("result") if isinstance(wrapper, dict) else None
        if isinstance(tweet_obj, dict):
            tweet_obj = self.processor._unwrap_tweet_result(tweet_obj)
            return self.processor._normalize_tweet(tweet_obj, source_endpoint="SearchTimeline")
        return None

    @staticmethod
    def _tweet_datetime(tweet: Dict[str, Any]) -> Optional[datetime]:
        parsed = parse_twitter_timestamp(tweet.get("raw_timestamp") or tweet.get("created_at"))
        return parsed.replace(tzinfo=None) if parsed else None

    def _parse_search_page(self, payload: Dict[str, Any], seen_ids: Set[str], capture_debug: bool) -> Dict[str, Any]:
        tweets: List[Dict[str, Any]] = []
        candidates: List[Dict[str, Any]] = []
        entry_type_counts: Dict[str, int] = defaultdict(int)
        skipped: List[Dict[str, Any]] = []
        processed: List[Dict[str, Any]] = []
        has_entries = False
        item_count = 0
        module_count = 0

        def add_tweet(wrapper: Dict[str, Any], entry_id: str, typename: str, source_path: str) -> None:
            parsed = self._parse_tweet_wrapper(wrapper)
            if not parsed:
                skipped.append({"entry_id": entry_id, "typename": typename, "reason": f"parse_failed:{source_path}"})
                return
            tweet_id = str(parsed.get("id") or "")
            if tweet_id and tweet_id not in seen_ids:
                seen_ids.add(tweet_id)
                tweets.append(parsed)
                processed.append({"entry_id": entry_id, "typename": typename, "tweet_id": tweet_id, "source_path": source_path, "status": "added"})
            elif tweet_id:
                processed.append({"entry_id": entry_id, "typename": typename, "tweet_id": tweet_id, "source_path": source_path, "status": "duplicate_ignored"})

        for inst in self._extract_instructions(payload):
            inst_type = str(inst.get("type", ""))
            entry_type_counts[f"instruction:{inst_type or 'unknown'}"] += 1
            entries = []
            if inst_type in {"TimelineReplaceEntry", "TimelinePinEntry"} and isinstance(inst.get("entry"), dict):
                entries = [inst["entry"]]
            elif inst_type == "TimelineAddEntries":
                entries = inst.get("entries", []) if isinstance(inst.get("entries"), list) else []
            else:
                skipped.append({"entry_id": "instruction", "typename": inst_type or "unknown", "reason": "unsupported_instruction_type"})
                continue
            has_entries = has_entries or bool(entries)
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                entry_id = str(entry.get("entryId", ""))
                content = entry.get("content", {}) if isinstance(entry.get("content"), dict) else {}
                typename = str(content.get("__typename", "unknown"))
                entry_type_counts[f"entry:{typename or 'unknown'}"] += 1
                candidates.extend(self._collect_cursor_candidates(entry, "timeline.entry"))
                item_content = content.get("itemContent", {}) if isinstance(content.get("itemContent"), dict) else {}
                if entry_id.startswith("tweet-") or typename == "TimelineTimelineItem":
                    item_count += 1
                    add_tweet(item_content.get("tweet_results", {}), entry_id, typename, "content.itemContent.tweet_results")
                    continue
                if isinstance(content.get("items"), list):
                    module_count += 1
                    for idx, module in enumerate(content["items"]):
                        module_item = module.get("item", {}) if isinstance(module, dict) else {}
                        module_content = module_item.get("itemContent", {}) if isinstance(module_item, dict) else {}
                        add_tweet(module_content.get("tweet_results", {}), f"{entry_id}#item{idx}", typename, "content.items.item.itemContent.tweet_results")
                    continue
                if "cursor" not in entry_id.lower():
                    skipped.append({"entry_id": entry_id, "typename": typename, "reason": "unsupported_entry_shape"})

        candidates = sorted(candidates, key=lambda row: int(row.get("score", 0)), reverse=True)
        next_cursor = str(candidates[0].get("value")) if candidates else None
        return {
            "tweets": tweets,
            "next_cursor": next_cursor,
            "has_entries": has_entries,
            "timeline_item_count": item_count,
            "timeline_module_count": module_count,
            "entry_type_counts": dict(entry_type_counts),
            "cursor_candidates": candidates,
            "selected_cursor_source": candidates[0].get("source_path") if candidates else None,
            "skipped_entries": skipped,
            "processed_entries": processed if capture_debug else [],
        }

    def _page_crossed_search_window(self, tweets: List[Dict[str, Any]], window_start: datetime) -> bool:
        dated = [self._tweet_datetime(tweet) for tweet in tweets]
        dated = [value for value in dated if value is not None]
        return bool(dated and max(dated) <= window_start)

    @staticmethod
    def classify_search_stall(
        *,
        cursor: Optional[str],
        next_cursor: Optional[str],
        has_entries: bool,
        new_items_count: int,
        cursor_history: Set[str],
    ) -> Optional[str]:
        """Classify SearchTimeline pagination stalls for testable skip/continue behavior."""
        if cursor and next_cursor and str(next_cursor) in cursor_history:
            return "repeated_cursor_history"
        if not next_cursor:
            return "no_bottom_cursor"
        return None

    @staticmethod
    def classify_search_failure(status_code: Optional[int], page: int, cursor: Optional[str]) -> str:
        """Classify request failures without raising the full monitor."""
        if status_code == 404 and cursor:
            return "mid_pagination_404"
        if page == 1:
            return "first_page_failed"
        if status_code == 429:
            return "rate_limited"
        if status_code:
            return f"http_{status_code}"
        return "request_failed"

    @staticmethod
    def _classify_http_failure(status_code: int, has_pages: bool, cursor_value: Optional[str]) -> str:
        if status_code == 404 and cursor_value and has_pages:
            return "partial_cursor_404"
        if status_code == 404:
            return "failed_initial_404"
        if status_code in {401, 403}:
            return "partial_http_error" if has_pages else "failed_initial_auth"
        if status_code == 429:
            return "partial_rate_limited" if has_pages else "failed_initial_rate_limit"
        if 500 <= status_code < 600:
            return "partial_http_error" if has_pages else "failed_initial_http_error"
        return "partial_http_error" if has_pages else "failed_initial_http_error"

    def _request_page(
        self,
        graphql_url: str,
        variables_template: Dict[str, Any],
        features_json: str,
        frozen_headers: Dict[str, str],
        cursor: Optional[str],
        retries: int,
        *,
        has_pages: bool = False,
        search_url: Optional[str] = None,
        browser_fallback_pages: int = 2,
    ) -> Dict[str, Any]:
        endpoint = "SearchTimeline"
        retry_policy = self.api_manager.retry_policy()
        max_attempts = max(
            max(1, retries),
            int(retry_policy.get("client_error_attempts", self.fetcher.max_cursor_error_retries)),
            int(retry_policy.get("server_error_attempts", self.fetcher.max_cursor_error_retries)),
            int(retry_policy.get("request_error_attempts", self.fetcher.max_cursor_error_retries)),
        )
        errors: List[Dict[str, Any]] = []
        attempts = 0
        auth_refreshed = False
        context_refreshed = False
        cursor_refreshed = False
        active_headers = dict(frozen_headers)
        for attempt in range(max_attempts):
            attempts += 1
            try:
                variables = dict(variables_template)
                if cursor:
                    variables["cursor"] = cursor
                params = {
                    "variables": self._compact_json(variables),
                    "features": features_json,
                }
                # #region agent log
                _agent_debug_log(
                    "A",
                    "search_timeline.py:_request_page",
                    "request_attempt",
                    {
                        "attempt": attempt + 1,
                        "has_cursor": bool(cursor),
                        "has_pages": has_pages,
                        "graphql_url_tail": graphql_url[-80:],
                    },
                )
                # #endregion
                response = self.api_manager.perform_get(
                    endpoint=endpoint,
                    url=graphql_url,
                    max_retries=1,
                    params=params,
                    headers=active_headers,
                )
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except Exception as exc:
                        return {
                            "_failure": "partial_parse_error" if has_pages else "failed_initial_parse_error",
                            "_status": response.status_code,
                            "_attempts": attempts,
                            "_error_samples": [{"cursor": cursor, "attempt": attempt + 1, "exception": f"JSON parse error: {str(exc)[:500]}"}],
                        }
                    validation = validate_graphql_payload(endpoint, payload)
                    if not validation.ok:
                        return {
                            "_failure": "partial_graphql_error" if has_pages else "failed_initial_graphql_error",
                            "_status": response.status_code,
                            "_attempts": attempts,
                            "_error_samples": [{
                                "cursor": cursor,
                                "attempt": attempt + 1,
                                "semantic_error": validation.reason,
                                "graphql_errors": validation.errors[:3],
                            }],
                        }
                    payload["_attempts"] = attempts
                    payload["_error_samples"] = errors[-5:]
                    payload["_status"] = response.status_code
                    return payload
                if response.status_code == 429:
                    errors.append({"status_code": 429, "cursor": cursor, "attempt": attempt + 1, "response_text": str(response.text or "")[:500]})
                    wait = self.api_manager.rate_limit_sleep_seconds(endpoint, response.headers)
                    if wait <= 0:
                        wait = int(retry_policy.get("rate_limit_safety_buffer_seconds", 5))
                    if attempt >= max_attempts - 1:
                        return {"_failure": self._classify_http_failure(429, has_pages, cursor), "_status": 429, "_attempts": attempts, "_error_samples": errors[-5:]}
                    time.sleep(wait)
                    continue
                if response.status_code in {400, 401, 403, 404}:
                    errors.append({"status_code": int(response.status_code), "cursor": cursor, "attempt": attempt + 1, "response_text": str(response.text or "")[:500]})
                    if response.status_code == 400:
                        return {"_failure": self._classify_http_failure(400, has_pages, cursor), "_status": 400, "_attempts": attempts, "_error_samples": errors[-5:]}
                    if response.status_code in {401, 403} and not auth_refreshed:
                        auth_refreshed = True
                        refreshed = self.fetcher.bootstrap_browser_context(search_url=search_url, max_pages=1)
                        if refreshed.ok:
                            self._after_bootstrap(refreshed, endpoint)
                            query_id = str(self.api_manager.get_query_id(endpoint) or "").strip()
                            if query_id:
                                graphql_url = f"https://x.com/i/api/graphql/{query_id}/{endpoint}"
                            if search_url:
                                active_headers = self._build_frozen_headers(search_url)
                            continue
                    if response.status_code == 404 and cursor and has_pages and not cursor_refreshed and search_url:
                        cursor_refreshed = True
                        refreshed = self.fetcher.bootstrap_browser_context(
                            search_url=search_url,
                            capture_endpoint=endpoint,
                            max_pages=2,
                        )
                        if refreshed.ok:
                            self._after_bootstrap(refreshed, endpoint)
                            query_id = str(self.api_manager.get_query_id(endpoint) or "").strip()
                            if query_id:
                                graphql_url = f"https://x.com/i/api/graphql/{query_id}/{endpoint}"
                            active_headers = self._build_frozen_headers(search_url)
                            # #region agent log
                            _agent_debug_log(
                                "A",
                                "search_timeline.py:_request_page",
                                "cursor_404_bootstrap",
                                {
                                    "bootstrap_ok": refreshed.ok,
                                    "query_id": refreshed.query_ids.get(endpoint),
                                    "endpoint_health": self.api_manager.get_endpoint_health(endpoint),
                                },
                            )
                            # #endregion
                            continue
                    if response.status_code == 404 and not cursor and not has_pages and not context_refreshed:
                        context_refreshed = True
                        refreshed = self.fetcher.bootstrap_browser_context(search_url=search_url, max_pages=1)
                        if refreshed.ok:
                            self._after_bootstrap(refreshed, endpoint)
                            query_id = str(self.api_manager.get_query_id(endpoint) or "").strip()
                            if query_id:
                                graphql_url = f"https://x.com/i/api/graphql/{query_id}/{endpoint}"
                            if search_url:
                                active_headers = self._build_frozen_headers(search_url)
                            continue
                    client_attempts = int(retry_policy.get("client_error_attempts", self.fetcher.max_cursor_error_retries))
                    if attempt < client_attempts - 1:
                        self.api_manager.jitter_sleep(
                            float(retry_policy.get("client_error_min_seconds", 10)),
                            float(retry_policy.get("client_error_max_seconds", 20)),
                            reason=f"SearchTimeline HTTP {response.status_code} retry {attempt + 1}/{client_attempts}",
                        )
                        continue
                    if response.status_code == 404 and not cursor and not has_pages and search_url:
                        fallback = self.fetcher.bootstrap_browser_context(
                            search_url=search_url,
                            capture_endpoint=endpoint,
                            max_pages=max(1, browser_fallback_pages),
                        )
                        if fallback.ok:
                            self._after_bootstrap(fallback, endpoint)
                        fallback_pages = fallback.target_pages.get(endpoint, []) if fallback.ok else []
                        valid_pages = [
                            page_payload for page_payload in fallback_pages
                            if validate_graphql_payload(endpoint, page_payload).ok
                        ]
                        if valid_pages:
                            return {
                                "_browser_pages": valid_pages,
                                "_failure": None,
                                "_status": 200,
                                "_attempts": attempts,
                                "_error_samples": errors[-5:],
                                "_transport": "browser_fallback",
                            }
                    return {"_failure": self._classify_http_failure(int(response.status_code), has_pages, cursor), "_status": int(response.status_code), "_attempts": attempts, "_error_samples": errors[-5:]}
                if 500 <= response.status_code < 600:
                    errors.append({"status_code": int(response.status_code), "cursor": cursor, "attempt": attempt + 1, "response_text": str(response.text or "")[:500]})
                    server_attempts = int(retry_policy.get("server_error_attempts", self.fetcher.max_cursor_error_retries))
                    if attempt < server_attempts - 1:
                        base = float(retry_policy.get("server_error_base_seconds", 5))
                        max_sleep = float(retry_policy.get("server_error_max_seconds", 60))
                        wait = min(max_sleep, base * (2 ** attempt))
                        self.api_manager.jitter_sleep(wait, wait + base, reason=f"SearchTimeline HTTP {response.status_code}")
                        continue
                    return {"_failure": self._classify_http_failure(int(response.status_code), has_pages, cursor), "_status": int(response.status_code), "_attempts": attempts, "_error_samples": errors[-5:]}
                errors.append({"status_code": int(response.status_code), "cursor": cursor, "attempt": attempt + 1, "response_text": str(response.text or "")[:500]})
                return {"_failure": self._classify_http_failure(int(response.status_code), has_pages, cursor), "_status": int(response.status_code), "_attempts": attempts, "_error_samples": errors[-5:]}
            except Exception as exc:
                errors.append({"cursor": cursor, "attempt": attempt + 1, "exception": str(exc)[:500]})
                request_attempts = int(retry_policy.get("request_error_attempts", self.fetcher.max_cursor_error_retries))
                if attempt < request_attempts - 1:
                    base = float(retry_policy.get("request_error_base_seconds", 5))
                    max_sleep = float(retry_policy.get("request_error_max_seconds", 60))
                    wait = min(max_sleep, base * (2 ** attempt))
                    self.api_manager.jitter_sleep(wait, wait + base, reason="SearchTimeline request error")
                    continue
                return {
                    "_failure": "partial_request_error" if has_pages else "failed_initial_request_error",
                    "_status": None,
                    "_attempts": attempts,
                    "_error_samples": errors[-5:],
                }
        return {
            "_failure": "partial_unknown_error" if has_pages else "failed_initial_unknown_error",
            "_status": None,
            "_attempts": attempts,
            "_error_samples": errors[-5:],
        }

    def _raw_batch_dir(self, slug: str, product: str) -> Path:
        target = self.raw_root / slug / product.lower() / self.storage._jalali_batch_name()
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _save_exports(self, slug: str, product: str, raw_query: str, tweets: List[Dict[str, Any]], debug: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, str]:
        target = self.processed_root / slug / product.lower()
        debug_target = self.debug_root / slug / product.lower()
        target.mkdir(parents=True, exist_ok=True)
        debug_target.mkdir(parents=True, exist_ok=True)
        dedup = {str(tweet.get("id")): tweet for tweet in tweets if tweet.get("id")}
        tweets_sorted = list(dedup.values())
        payload = {"generated_at": datetime.utcnow().isoformat() + "Z", "search_slug": slug, "product": product, "raw_query": raw_query, "metadata": metadata, "tweets": tweets_sorted}
        json_path = target / f"{slug}.json"
        txt_path = target / f"{slug}.txt"
        self._save_json(json_path, payload)
        self.storage.save_processed_txt(tweets_sorted, txt_path)
        for name in ["entry_type_counts", "cursor_candidates", "skipped_entries", "processed_entries"]:
            self._save_json(debug_target / f"{slug}__debug_first_page_{name}.json", debug.get(name, {} if name == "entry_type_counts" else []))
        return {"json": str(json_path), "txt": str(txt_path), "debug_dir": str(debug_target)}

    def _state_key(self, search_def: Dict[str, Any], product: str) -> str:
        return f"{SearchQueryBuilder.slug(search_def)}::{product.lower()}"

    def should_stop_search_pagination(
        self,
        *,
        page_result: Dict[str, Any],
        window_start: datetime,
        cursor: Optional[str],
        cursor_history: Set[str],
    ) -> Tuple[bool, Optional[str]]:
        next_cursor = page_result.get("next_cursor")
        if cursor and next_cursor and str(next_cursor) in cursor_history:
            return True, "repeated_cursor_history"
        if not next_cursor:
            return True, "no_bottom_cursor"
        return False, None

    def should_fetch_search(self, search_def: Dict[str, Any], product: str, interval_seconds: int, force_run: bool = False) -> bool:
        # اگر در حالت force_run هستیم، تایمرها کاملا نادیده گرفته می‌شوند
        if force_run:
            return True
            
        state = self.search_state.get(self._state_key(search_def, product), {})
        last = state.get("last_checked_at") if isinstance(state, dict) else None
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", ""))
        except Exception:
            return True
        return (datetime.utcnow() - last_dt).total_seconds() >= interval_seconds

    def monitor_search(self, search_def: Dict[str, Any]) -> Dict[str, Any]:
        product = SearchQueryBuilder.normalize_product(str(search_def.get("product", "Top")))
        slug = SearchQueryBuilder.slug(search_def)
        raw_query = SearchQueryBuilder.build_raw_query(search_def, self.storage._tehran_now())
        search_url = SearchQueryBuilder.build_human_search_url(raw_query, product)
        policy = self._policy_for_search(search_def)
        jalali_batch = self.storage._jalali_batch_name()
        batch_dir = self.raw_root / slug / product.lower() / jalali_batch
        batch_dir.mkdir(parents=True, exist_ok=True)
        self.console.info(f"Fetching search: {search_def.get('name', slug)} (product={product})")
        self.console.info(f"  Query: {raw_query}")
        seen_ids: Set[str] = set()
        cursor: Optional[str] = None
        cursor_history: Set[str] = set()
        tweets: List[Dict[str, Any]] = []
        debug: Dict[str, Any] = {}
        attempts = 0
        error_samples: List[Dict[str, Any]] = []
        last_http_status: Optional[int] = None
        exhausted_reason = "unknown"
        transport = "http"
        page_output_paths: List[str] = []
        rolling_hours = int(policy["rolling_hours"])
        window_start = datetime.utcnow() - timedelta(hours=max(1, rolling_hours))
        variables_template = self._build_base_variables(search_def, raw_query, product)
        search_features = (
            self.config.get("graphql_endpoint_payloads", {})
            .get("SearchTimeline", {})
            .get("features", FROZEN_SEARCH_FEATURES)
        )
        features_json = self._compact_json(dict(search_features))

        bootstrap = self.fetcher.bootstrap_browser_context(search_url=search_url, max_pages=1)
        self._after_bootstrap(bootstrap, "SearchTimeline")
        # #region agent log
        _agent_debug_log(
            "B",
            "search_timeline.py:monitor_search",
            "startup_bootstrap",
            {
                "bootstrap_ok": bootstrap.ok,
                "query_id": str(self.api_manager.get_query_id("SearchTimeline") or ""),
                "endpoint_health": self.api_manager.get_endpoint_health("SearchTimeline"),
                "query_id_pool_size": len(self.api_manager.query_id_pools.get("SearchTimeline", [])),
                "page_cap": int(policy["pagination_safety_cap_pages"]),
            },
        )
        # #endregion
        query_id = str(self.api_manager.get_query_id("SearchTimeline") or "").strip()
        if not query_id:
            raise RuntimeError("Missing api_config.search_timeline_query_id")
        self.api_manager.warmup_url(search_url, timeout=int(self.config.get("api_config", {}).get("search_warmup_seconds", 30)))
        if self.fetcher.first_request_warmup_seconds > 0:
            time.sleep(self.fetcher.first_request_warmup_seconds)
        for page in range(1, int(policy["pagination_safety_cap_pages"]) + 1):
            query_id = str(self.api_manager.get_query_id("SearchTimeline") or "").strip()
            graphql_url = f"https://x.com/i/api/graphql/{query_id}/SearchTimeline"
            frozen_headers = self._build_frozen_headers(search_url)
            payload = self._request_page(
                graphql_url,
                variables_template,
                features_json,
                frozen_headers,
                cursor,
                int(policy["max_retries"]),
                has_pages=bool(tweets),
                search_url=search_url,
                browser_fallback_pages=min(2, int(policy["pagination_safety_cap_pages"])),
            )
            attempts += int(payload.get("_attempts", 0) or 0)
            last_http_status = payload.get("_status", last_http_status)
            error_samples.extend(payload.get("_error_samples", []) if isinstance(payload.get("_error_samples"), list) else [])
            browser_pages = payload.pop("_browser_pages", None)
            if browser_pages:
                transport = "browser_fallback"
                for fallback_index, fallback_payload in enumerate(browser_pages, start=page):
                    output_path = self.storage.save_search_result_page(slug, product, jalali_batch, fallback_index, fallback_payload)
                    page_output_paths.append(str(output_path))
                    page_result = self._parse_search_page(fallback_payload, seen_ids, capture_debug=(fallback_index == 1))
                    tweets.extend(page_result["tweets"])
                    if fallback_index == 1:
                        debug = {key: page_result.get(key) for key in ["entry_type_counts", "cursor_candidates", "skipped_entries", "processed_entries", "selected_cursor_source"]}
                    next_cursor = page_result.get("next_cursor") or extract_bottom_cursor(fallback_payload)
                    cursor = str(next_cursor) if next_cursor else None
                exhausted_reason = "verified_browser_fallback"
                break
            if payload.get("_failure"):
                exhausted_reason = str(payload["_failure"])
                if exhausted_reason == "partial_cursor_404" and tweets and search_url:
                    remaining = max(1, int(policy["pagination_safety_cap_pages"]) - page + 1)
                    fallback = self.fetcher.bootstrap_browser_context(
                        search_url=search_url,
                        capture_endpoint="SearchTimeline",
                        max_pages=min(2, remaining),
                    )
                    if fallback.ok:
                        self._after_bootstrap(fallback, "SearchTimeline")
                    valid_pages = [
                        page_payload
                        for page_payload in (fallback.target_pages.get("SearchTimeline", []) if fallback.ok else [])
                        if validate_graphql_payload("SearchTimeline", page_payload).ok
                    ]
                    if valid_pages:
                        transport = "browser_fallback"
                        for offset, fallback_payload in enumerate(valid_pages):
                            page_number = page + offset
                            output_path = self.storage.save_search_result_page(
                                slug, product, jalali_batch, page_number, fallback_payload
                            )
                            page_output_paths.append(str(output_path))
                            page_result = self._parse_search_page(fallback_payload, seen_ids, capture_debug=False)
                            tweets.extend(page_result["tweets"])
                        exhausted_reason = "verified_browser_fallback"
                        # #region agent log
                        _agent_debug_log(
                            "A",
                            "search_timeline.py:monitor_search",
                            "partial_cursor_browser_recovery",
                            {
                                "recovered_pages": len(valid_pages),
                                "tweets_after_recovery": len(tweets),
                                "starting_page": page,
                            },
                        )
                        # #endregion
                        break
                # #region agent log
                _agent_debug_log(
                    "A",
                    "search_timeline.py:monitor_search",
                    "pagination_failure",
                    {
                        "page": page,
                        "failure": exhausted_reason,
                        "last_http_status": payload.get("_status"),
                        "tweets_so_far": len(tweets),
                    },
                )
                # #endregion
                break
            payload.pop("_attempts", None)
            payload.pop("_error_samples", None)
            payload.pop("_status", None)
            output_path = self.storage.save_search_result_page(slug, product, jalali_batch, page, payload)
            page_output_paths.append(str(output_path))
            page_result = self._parse_search_page(payload, seen_ids, capture_debug=(page == 1))
            tweets.extend(page_result["tweets"])
            self.console.info(
                f"  Page {page} fetched; tweets={len(page_result['tweets'])} "
                f"next_cursor={'yes' if page_result.get('next_cursor') else 'no'}"
            )
            if page == 1:
                debug = {key: page_result.get(key) for key in ["entry_type_counts", "cursor_candidates", "skipped_entries", "processed_entries", "selected_cursor_source"]}
            next_cursor = page_result.get("next_cursor")
            if self._page_crossed_search_window(page_result["tweets"], window_start):
                self.console.info(
                    f"  Page {page} is older than the rolling window, but the cursor is still active so pagination continues."
                )
            stop_pagination, stop_reason = self.should_stop_search_pagination(
                page_result=page_result,
                window_start=window_start,
                cursor=cursor,
                cursor_history=cursor_history,
            )
            if stop_pagination:
                exhausted_reason = stop_reason or "pagination_stopped"
                break
            cursor_history.add(str(next_cursor))
            cursor = str(next_cursor)
            self.api_manager.human_delay("between_pages")
        else:
            exhausted_reason = "partial_safety_cap_reached"

        pages_on_disk = len(list(batch_dir.glob("page_*.json")))
        # #region agent log
        _agent_debug_log(
            "C",
            "search_timeline.py:monitor_search",
            "run_complete",
            {
                "exhausted_reason": exhausted_reason,
                "pages_saved_disk": pages_on_disk,
                "pages_saved_paths": len(page_output_paths),
                "batch_dir": str(batch_dir),
                "tweets": len(tweets),
                "endpoint_health": self.api_manager.get_endpoint_health("SearchTimeline"),
            },
        )
        # #endregion
        metadata = {
            "pages_requested": int(policy["pagination_safety_cap_pages"]),
            "pages_saved": pages_on_disk,
            "exhausted_reason": exhausted_reason,
            "cursor_history": sorted(cursor_history),
            "raw_batch_path": str(batch_dir),
            "rolling_hours": rolling_hours,
            "window_start_utc": window_start.isoformat() + "Z",
            "attempts": attempts,
            "last_http_status": last_http_status,
            "error_samples": error_samples[-5:],
            "transport": transport,
            "bootstrap_route": bootstrap.route,
            "browser_bootstrap": {
                "ok": bootstrap.ok,
                "support_request_count": bootstrap.support_request_count,
                "error": bootstrap.error,
            },
            "rate_headers": self.api_manager.rate_limits.get("SearchTimeline", {}),
            "output_paths": {"raw_pages": page_output_paths},
        }
        outputs = self._save_exports(slug, product, raw_query, tweets, debug, metadata)
        successful_reasons = {"success_search_window_crossed", "no_bottom_cursor", "verified_browser_fallback"}
        status = "completed" if exhausted_reason in successful_reasons else ("partial" if tweets else "failed")
        endpoint_status = "verified_browser_fallback" if transport == "browser_fallback" and status == "completed" else ("verified_http" if status == "completed" else "failed")
        report = {
            "search": search_def.get("name", slug),
            "slug": slug,
            "product": product,
            "raw_query": raw_query,
            "status": status,
            "endpoint_status": endpoint_status,
            "metadata": metadata,
            "counts": {"tweets": len(tweets)},
            "outputs": outputs,
        }
        
        # ------------- تغییرات جدید سیستم State -------------
        state_key = self._state_key(search_def, product)
        current_state = self.search_state.get(state_key, {})
        
        # تشخیص اینکه آیا چرخه واقعاً موفق بوده یا به خاطر ارور متوقف شده (مثل 401، 403، 404)
        is_success = status == "completed"
        
        new_state = {
            "last_status": exhausted_reason if is_success else f"error_http_{last_http_status}",
            "last_counts": report["counts"]
        }
        
        if is_success:
            # در صورت موفقیت کامل، زمان آپدیت می‌شود تا سیستم طبق Policy به خواب برود
            new_state["last_checked_at"] = datetime.utcnow().isoformat() + "Z"
        else:
            # در صورت بروز خطا، زمان قبلی حفظ می‌شود تا در رانِ بعدی فوراً مجدداً تلاش کند
            new_state["last_checked_at"] = current_state.get("last_checked_at")
            
        self.search_state[state_key] = new_state
        self._save_json(self.state_file, self.search_state)
        # --------------------------------------------------

        self._save_json(self.reports_root / f"{slug}_{product.lower()}_{self.storage._jalali_batch_name()}.json", report)
        return report

    def run_cycle(self, only_names: Optional[Set[str]] = None, force_run: bool = False) -> List[Dict[str, Any]]:
        self.fetcher.recorder.emit(
            "cycle_start",
            searches=sorted(only_names) if only_names else "all",
            force_run=force_run,
        )
        reports = []
        for search_def in self.search_defs:
            if not search_def.get("enabled", True):
                continue
            name = str(search_def.get("name", ""))
            if only_names and name not in only_names and SearchQueryBuilder.slug(search_def) not in only_names:
                continue
            product = SearchQueryBuilder.normalize_product(str(search_def.get("product", "Top")))
            policy = self._policy_for_search(search_def)
            
            # پارامتر force_run اینجا به تابع چک‌کننده پاس داده می‌شود
            if not self.should_fetch_search(
                search_def,
                product,
                int(policy["poll_interval_seconds"]),
                force_run=force_run or bool(self.validation_run_id),
            ):
                continue
                
            reports.append(self.monitor_search(search_def))
        self.fetcher.recorder.emit(
            "cycle_end",
            searches_fetched=len(reports),
            statuses=[report.get("status") for report in reports],
        )
        return reports

    def _print_cycle_summary(self, reports: List[Dict[str, Any]], only_names: Optional[Set[str]]) -> None:
        """Print a summary after a search cycle completes."""
        self.console.banner(f"Cycle complete: {len(reports)} search(es) fetched")
        for report in reports:
            self.console.search_summary(report)
        if not reports:
            self.console.warning("No searches were fetched in this cycle")
        if only_names:
            self.console.info(f"Note: --only filter active: {', '.join(only_names)}")

    def run_continuous(self, only_names: Optional[Set[str]] = None, check_interval: int = 60) -> None:
        self.console.banner("Starting v4 SearchTimeline monitor. Press Ctrl+C to stop.")
        while True:
            reports = self.run_cycle(only_names=only_names)
            self._print_cycle_summary(reports, only_names)
            time.sleep(max(1, check_interval))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run isolated v4 SearchTimeline monitoring.")
    parser.add_argument("--config")
    parser.add_argument("--search-config")
    parser.add_argument("--only", action="append", help="Limit to search name/slug; can be repeated.")
    parser.add_argument("--once", action="store_true", help="Run one validation cycle instead of continuous mode.")
    parser.add_argument("--check-interval", type=int, default=60)
    parser.add_argument("--validation-run-id", help="Write isolated output under data/validation/<run_id>/ and bypass polling state.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    monitor = SearchTimelineMonitor(
        config_path=args.config,
        search_config_path=args.search_config,
        validation_run_id=args.validation_run_id,
    )
    only = set(args.only or []) or None
    if args.once:
        reports = monitor.run_cycle(only_names=only)
        monitor._print_cycle_summary(reports, only)
    else:
        monitor.run_continuous(only_names=only, check_interval=args.check_interval)


if __name__ == "__main__":
    main()
