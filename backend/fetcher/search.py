#!/usr/bin/env python3
"""SearchTimeline query monitor.

Runs configured search queries (from ``config/searches.json``), fetches pages per
product, parses tweets/cursors, and writes raw + processed exports under
``data/search/``.

Run:
    tdf-search --once
    python -m fetcher.search --once

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
import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
except Exception:
    Console = None
    Panel = None
    Table = None


from fetcher.config import PROJECT_ROOT

from fetcher.config import DEFAULT_PRIORITY_POLICIES
from fetcher.timeline import FetcherEngine
from fetcher.processing import (
    SEARCH_TIMELINE_FEATURES,
    TweetSetProcessor,
    extract_bottom_cursor,
    parse_twitter_timestamp,
    search_timeline_variables,
    validate_graphql_payload,
)
from fetcher.storage import StorageManager
from fetcher.observability import PipelineConsole
from fetcher.observability import redact_exception
from fetcher.observability import attach_run_id
from fetcher.config import resolve_config_path


VALID_PRODUCTS = {"Top", "Latest", "Media", "People"}

FROZEN_SEARCH_FEATURES: Dict[str, object] = dict(SEARCH_TIMELINE_FEATURES)
logger = logging.getLogger(__name__)


def _cursor_ref(cursor: Optional[str]) -> Optional[str]:
    return hashlib.sha256(str(cursor).encode("utf-8")).hexdigest()[:12] if cursor else None


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
    ):
        self.project_root = PROJECT_ROOT
        self.fetcher = FetcherEngine(config_path=config_path, subsystem="search")
        self.run_id = self.fetcher.storage_manager.create_run_id()
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
        requested_depth = max(1, int(search_def.get("pagination_depth", 1)))
        if search_def.get("pagination_safety_cap_pages") is not None:
            page_cap = int(search_def["pagination_safety_cap_pages"])
        else:
            page_cap = default_cap
        return {
            "poll_interval_seconds": int(search_def.get("poll_interval_seconds", defaults["poll_interval_seconds"])),
            "pagination_depth": requested_depth,
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
        # APIManager supplies auth/session headers and the endpoint-pinned browser
        # transaction ID. Freezing the whole session here used to overwrite that
        # confirmed ID with a stale cross-endpoint value and caused page-1 404s.
        return {"referer": search_url, "x-twitter-active-user": "yes"}

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

    def _deep_stop_predicate(
        self, window_start: datetime, known_ground: Optional[datetime]
    ) -> Any:
        """Decide when to stop scrolling for deeper pages.

        Deep search pages come from a real browser scrolling the results page,
        so the cost here is minutes of wall clock, not API quota -- and the run
        is bounded by a wall-clock timeout, which is why the last query of a
        multi-query cycle used to be killed every time. Two things make further
        pages worthless: the rolling window has been crossed, or the page is
        entirely at or older than the newest tweet the previous run already
        stored. The second turns a repoll from dozens of scrolls into one or two.

        Only meaningful on `Latest`. `Top` is relevance-ranked, so an old tweet
        on page 1 says nothing about what page 2 holds.
        """
        seen: Set[str] = set()

        def stop(payload: Dict[str, Any]) -> bool:
            if not validate_graphql_payload("SearchTimeline", payload).ok:
                return False
            tweets = self._parse_search_page(payload, seen, capture_debug=False)["tweets"]
            if self._page_crossed_search_window(tweets, window_start):
                return True
            return known_ground is not None and self._page_crossed_search_window(tweets, known_ground)

        return stop

    @staticmethod
    def _parse_known_ground(state: Dict[str, Any]) -> Optional[datetime]:
        """The newest tweet a previous successful run of this search stored."""
        raw = state.get("newest_seen_at") if isinstance(state, dict) else None
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", ""))
        except ValueError:
            return None

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
        account: str = "search",
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
        context_refreshed = False
        cursor_refreshed = False
        active_headers = dict(frozen_headers)
        for attempt in range(max_attempts):
            attempts += 1
            request_started = time.monotonic()
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
                latency_ms = int((time.monotonic() - request_started) * 1000)
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except Exception as exc:
                        return {
                            "_failure": "partial_parse_error" if has_pages else "failed_initial_parse_error",
                            "_status": response.status_code,
                            "_attempts": attempts,
                            "_error_samples": [{"cursor": _cursor_ref(cursor), "attempt": attempt + 1, "exception": f"JSON parse error: {str(exc)[:500]}"}],
                        }
                    validation = validate_graphql_payload(endpoint, payload)
                    if not validation.ok:
                        return {
                            "_failure": "partial_graphql_error" if has_pages else "failed_initial_graphql_error",
                            "_status": response.status_code,
                            "_attempts": attempts,
                            "_error_samples": [{
                                "cursor": _cursor_ref(cursor),
                                "attempt": attempt + 1,
                                "semantic_error": validation.reason,
                                "graphql_errors": validation.errors[:3],
                            }],
                        }
                    payload["_attempts"] = attempts
                    payload["_error_samples"] = errors[-5:]
                    payload["_status"] = response.status_code
                    payload["_latency_ms"] = latency_ms
                    if errors:
                        self.fetcher.recorder.mark_http_recovered(account, endpoint)
                    return payload
                self.fetcher.recorder.emit_http_error(
                    account=account,
                    endpoint=endpoint,
                    status_code=int(response.status_code),
                    cursor=cursor,
                    request_url=graphql_url,
                    request_headers=active_headers,
                    variables=variables,
                    response_text=str(response.text or ""),
                )
                if response.status_code == 429:
                    errors.append({"status_code": 429, "cursor": _cursor_ref(cursor), "attempt": attempt + 1, "response_text": str(response.text or "")[:500]})
                    wait = self.api_manager.rate_limit_sleep_seconds(endpoint, response.headers)
                    if wait <= 0:
                        wait = int(retry_policy.get("rate_limit_safety_buffer_seconds", 5))
                    if attempt >= max_attempts - 1:
                        return {"_failure": self._classify_http_failure(429, has_pages, cursor), "_status": 429, "_attempts": attempts, "_error_samples": errors[-5:]}
                    time.sleep(wait)
                    continue
                if response.status_code in {400, 401, 403, 404}:
                    errors.append({"status_code": int(response.status_code), "cursor": _cursor_ref(cursor), "attempt": attempt + 1, "response_text": str(response.text or "")[:500]})
                    if response.status_code == 400:
                        return {"_failure": self._classify_http_failure(400, has_pages, cursor), "_status": 400, "_attempts": attempts, "_error_samples": errors[-5:]}
                    if response.status_code in {401, 403}:
                        return {
                            "_failure": self._classify_http_failure(response.status_code, has_pages, cursor),
                            "_status": response.status_code,
                            "_attempts": attempts,
                            "_error_samples": errors[-5:],
                            "_auth_required": True,
                        }
                    if response.status_code == 404 and cursor and has_pages:
                        # Known server-side SearchTimeline cursor gate — do not burn HTTP retries.
                        return {
                            "_failure": self._classify_http_failure(404, has_pages, cursor),
                            "_status": 404,
                            "_attempts": attempts,
                            "_error_samples": errors[-5:],
                            "_cursor_gate": True,
                        }
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
                    # Initial SearchTimeline 404: one quick path to browser, no multi-second HTTP retry loop.
                    if response.status_code == 404 and not cursor and not has_pages and search_url and browser_fallback_pages > 0:
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
                        return {
                            "_failure": self._classify_http_failure(404, has_pages, cursor),
                            "_status": 404,
                            "_attempts": attempts,
                            "_error_samples": errors[-5:],
                        }
                    client_attempts = int(retry_policy.get("client_error_attempts", self.fetcher.max_cursor_error_retries))
                    if attempt < client_attempts - 1 and response.status_code != 404:
                        self.api_manager.jitter_sleep(
                            float(retry_policy.get("client_error_min_seconds", 10)),
                            float(retry_policy.get("client_error_max_seconds", 20)),
                            reason=f"SearchTimeline HTTP {response.status_code} retry {attempt + 1}/{client_attempts}",
                        )
                        continue
                    return {"_failure": self._classify_http_failure(int(response.status_code), has_pages, cursor), "_status": int(response.status_code), "_attempts": attempts, "_error_samples": errors[-5:]}
                if 500 <= response.status_code < 600:
                    errors.append({"status_code": int(response.status_code), "cursor": _cursor_ref(cursor), "attempt": attempt + 1, "response_text": str(response.text or "")[:500]})
                    server_attempts = int(retry_policy.get("server_error_attempts", self.fetcher.max_cursor_error_retries))
                    if attempt < server_attempts - 1:
                        base = float(retry_policy.get("server_error_base_seconds", 5))
                        max_sleep = float(retry_policy.get("server_error_max_seconds", 60))
                        wait = min(max_sleep, base * (2 ** attempt))
                        self.api_manager.jitter_sleep(wait, wait + base, reason=f"SearchTimeline HTTP {response.status_code}")
                        continue
                    return {"_failure": self._classify_http_failure(int(response.status_code), has_pages, cursor), "_status": int(response.status_code), "_attempts": attempts, "_error_samples": errors[-5:]}
                errors.append({"status_code": int(response.status_code), "cursor": _cursor_ref(cursor), "attempt": attempt + 1, "response_text": str(response.text or "")[:500]})
                return {"_failure": self._classify_http_failure(int(response.status_code), has_pages, cursor), "_status": int(response.status_code), "_attempts": attempts, "_error_samples": errors[-5:]}
            except Exception as exc:
                safe_exception = redact_exception(exc)
                errors.append({"cursor": _cursor_ref(cursor), "attempt": attempt + 1, "exception": safe_exception})
                self.fetcher.recorder.emit(
                    "request_error",
                    account=account,
                    endpoint=endpoint,
                    cursor=_cursor_ref(cursor),
                    attempt=attempt + 1,
                    exception_type=type(exc).__name__,
                    exception=safe_exception,
                )
                request_attempts = int(retry_policy.get("request_error_attempts", self.fetcher.max_cursor_error_retries))
                if attempt < request_attempts - 1:
                    base = float(retry_policy.get("request_error_base_seconds", 5))
                    max_sleep = float(retry_policy.get("request_error_max_seconds", 60))
                    wait = min(max_sleep, base * (2 ** attempt))
                    self.api_manager.jitter_sleep(
                        wait,
                        wait + base,
                        reason=f"SearchTimeline request error ({type(exc).__name__})",
                    )
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
        target = self.raw_root / slug / product.lower() / self.storage._batch_name()
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
        self._save_json(json_path, payload)
        for name in ["entry_type_counts", "cursor_candidates", "skipped_entries", "processed_entries"]:
            self._save_json(debug_target / f"{slug}__debug_first_page_{name}.json", debug.get(name, {} if name == "entry_type_counts" else []))
        return {"json": str(json_path), "debug_dir": str(debug_target)}

    def _state_key(self, search_def: Dict[str, Any], product: str) -> str:
        return f"{SearchQueryBuilder.slug(search_def)}::{product.lower()}"

    def should_stop_search_pagination(
        self,
        *,
        page_result: Dict[str, Any],
        window_start: datetime,
        cursor: Optional[str],
        cursor_history: Set[str],
        known_ground: Optional[datetime] = None,
    ) -> Tuple[bool, Optional[str]]:
        next_cursor = page_result.get("next_cursor")
        if self._page_crossed_search_window(page_result.get("tweets", []), window_start):
            return True, "success_search_window_crossed"
        # Reaching what the last successful run already stored is a success, and
        # has to be recognised here as well as in the browser's scroll predicate.
        # Without this clause the page loop never breaks for that reason, the
        # for/else labels the run "partial_browser_predicate", and a partial run
        # is not allowed to advance `newest_seen_at` -- so the very optimization
        # that stopped early could never move the mark it stops at, and every
        # later poll would scroll back to the same ageing boundary.
        if known_ground is not None and self._page_crossed_search_window(
            page_result.get("tweets", []), known_ground
        ):
            return True, "success_reached_known_ground"
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
        raw_query = SearchQueryBuilder.build_raw_query(search_def, self.storage._now())
        search_url = SearchQueryBuilder.build_human_search_url(raw_query, product)
        policy = self._policy_for_search(search_def)
        jalali_batch = self.storage._batch_name()
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
        # Where the previous successful run of this search got to. Pages entirely
        # older than this are already stored, so scrolling to them is wasted time.
        known_ground = self._parse_known_ground(
            self.search_state.get(self._state_key(search_def, product), {})
        )
        variables_template = self._build_base_variables(search_def, raw_query, product)
        search_features = (
            self.config.get("graphql_endpoint_payloads", {})
            .get("SearchTimeline", {})
            .get("features", FROZEN_SEARCH_FEATURES)
        )
        features_json = self._compact_json(dict(search_features))

        page_cap = int(policy["pagination_safety_cap_pages"])
        requested_depth = max(1, int(policy.get("pagination_depth", search_def.get("pagination_depth", 1))))
        deep_search = requested_depth > 1
        bootstrap = None
        payloads: List[Dict[str, Any]] = []

        query_id = str(self.api_manager.get_query_id("SearchTimeline") or "").strip()
        if not query_id:
            raise RuntimeError("Missing api_config.search_timeline_query_id")

        # Always take HTTP page 1 (~1s). HTTP cursor pages 404 server-side — browser only for depth.
        http_payload = self._request_page(
            f"https://x.com/i/api/graphql/{query_id}/SearchTimeline",
            variables_template,
            features_json,
            self._build_frozen_headers(search_url),
            None,
            int(policy["max_retries"]),
            search_url=search_url,
            browser_fallback_pages=1 if not deep_search else 0,
            account=slug,
        )
        attempts = int(http_payload.pop("_attempts", 0) or 0)
        last_http_status = http_payload.pop("_status", None)
        http_latency_ms = http_payload.pop("_latency_ms", None)
        error_samples.extend(http_payload.pop("_error_samples", []))
        http_browser_pages = http_payload.pop("_browser_pages", None)
        http_transport = http_payload.pop("_transport", None)
        http_failure = http_payload.pop("_failure", None)

        if http_browser_pages:
            payloads = list(http_browser_pages)[:page_cap]
            transport = str(http_transport or "browser_fallback")
            exhausted_reason = "depth_one_complete" if not deep_search else "partial_browser_fallback"
            self.fetcher.recorder.mark_http_recovered(slug, "SearchTimeline")
        elif not http_failure:
            payloads = [http_payload]
            transport = "http"
            if not deep_search:
                exhausted_reason = "depth_one_complete"
            else:
                # The stop tests are CHRONOLOGICAL, so they are only meaningful on
                # `Latest`. `Top` is relevance-ranked: its page 1 routinely contains
                # tweets older than the window, which tripped this predicate on the
                # very first page and capped every deep Top search at one page.
                # Depth on Top is bounded by page_cap instead.
                chronological = SearchQueryBuilder.normalize_product(product) == "Latest"
                # HTTP p1 kept; browser supplies deeper pages (skip duplicate first capture when possible).
                bootstrap = self.fetcher.bootstrap_browser_context(
                    search_url=search_url,
                    capture_endpoint="SearchTimeline",
                    max_pages=page_cap,
                    stop_when=self._deep_stop_predicate(window_start, known_ground) if chronological else None,
                )
                self._after_bootstrap(bootstrap, "SearchTimeline")
                attempts += len(bootstrap.target_pages.get("SearchTimeline", []))
                valid_pages = [
                    page_payload
                    for page_payload in bootstrap.target_pages.get("SearchTimeline", [])
                    if validate_graphql_payload("SearchTimeline", page_payload).ok
                ]
                if bootstrap.ok and len(valid_pages) > 1:
                    payloads = [http_payload] + valid_pages[1:page_cap]
                    transport = "http+browser"
                    self.fetcher.recorder.mark_http_recovered(slug, "SearchTimeline")
                elif bootstrap.ok and valid_pages:
                    # Browser only re-captured page 1 — keep fast HTTP page.
                    payloads = [http_payload]
                    transport = "http"
                elif not bootstrap.ok:
                    error_samples.append({"transport": "browser", "exception": bootstrap.error})
        else:
            exhausted_reason = str(http_failure)
            if deep_search:
                transport = "browser"
                # Same chronological-only gate as the HTTP-success path above:
                # these stops are meaningless on relevance-ranked `Top`.
                chronological = product == "Latest"
                bootstrap = self.fetcher.bootstrap_browser_context(
                    search_url=search_url,
                    capture_endpoint="SearchTimeline",
                    max_pages=page_cap,
                    stop_when=self._deep_stop_predicate(window_start, known_ground) if chronological else None,
                )
                self._after_bootstrap(bootstrap, "SearchTimeline")
                attempts += len(bootstrap.target_pages.get("SearchTimeline", []))
                valid_pages = [
                    page_payload
                    for page_payload in bootstrap.target_pages.get("SearchTimeline", [])
                    if validate_graphql_payload("SearchTimeline", page_payload).ok
                ]
                if bootstrap.ok and valid_pages:
                    payloads = valid_pages[:page_cap]
                    last_http_status = 200
                    exhausted_reason = "unknown"
                    self.fetcher.recorder.mark_http_recovered(slug, "SearchTimeline")
                else:
                    exhausted_reason = f"failed_browser_{bootstrap.stop_reason or 'capture'}"
                    if bootstrap.error:
                        error_samples.append({"transport": "browser", "exception": bootstrap.error})

        for page, payload in enumerate(payloads, start=1):
            output_path = self.storage.save_search_result_page(slug, product, jalali_batch, page, payload)
            page_output_paths.append(str(output_path))
            page_result = self._parse_search_page(payload, seen_ids, capture_debug=(page == 1))
            tweets.extend(page_result["tweets"])
            next_cursor = page_result.get("next_cursor") or extract_bottom_cursor(payload)
            page_transport = "http" if page == 1 and transport == "http+browser" else (
                "browser" if transport in {"browser", "http+browser", "browser_fallback"} else transport
            )
            page_latency_ms = http_latency_ms if page == 1 and page_transport == "http" else None
            self.console.page_row(
                account=slug,
                endpoint="SearchTimeline",
                page=page,
                transport=page_transport,
                items=len(page_result["tweets"]),
                cursor_status="found" if next_cursor else "end",
                http_status=200,
                latency_ms=page_latency_ms,
                next_page=page + 1 if next_cursor and page < len(payloads) else None,
            )
            self.fetcher.recorder.emit_page_fetched(
                account=slug,
                endpoint="SearchTimeline",
                page=page,
                cursor_in=cursor,
                cursor_out=str(next_cursor) if next_cursor else None,
                http_status=200,
                items=len(page_result["tweets"]),
                transport=page_transport,
                latency_ms=page_latency_ms,
            )
            if page == 1:
                debug = {key: page_result.get(key) for key in ["entry_type_counts", "cursor_candidates", "skipped_entries", "processed_entries", "selected_cursor_source"]}
            stop_pagination, stop_reason = self.should_stop_search_pagination(
                page_result=page_result,
                window_start=window_start,
                cursor=cursor,
                cursor_history=cursor_history,
                known_ground=known_ground,
            )
            if stop_pagination:
                exhausted_reason = stop_reason or "pagination_stopped"
                break
            cursor_history.add(str(next_cursor))
            cursor = str(next_cursor)
        else:
            if payloads and not deep_search:
                exhausted_reason = "depth_one_complete"
            elif payloads and len(payloads) >= page_cap:
                exhausted_reason = "partial_safety_cap_reached"
            elif payloads:
                exhausted_reason = f"partial_browser_{bootstrap.stop_reason or 'stalled'}"

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
            "pages_requested": requested_depth,
            "safety_cap_pages": page_cap,
            "pages_saved": pages_on_disk,
            "exhausted_reason": exhausted_reason,
            "cursor_history": sorted(filter(None, (_cursor_ref(value) for value in cursor_history))),
            "raw_batch_path": str(batch_dir),
            "rolling_hours": rolling_hours,
            "window_start_utc": window_start.isoformat() + "Z",
            "attempts": attempts,
            "last_http_status": last_http_status,
            "error_samples": error_samples[-5:],
            "transport": transport,
            "bootstrap_route": bootstrap.route if bootstrap else None,
            "browser_bootstrap": {
                "ok": bootstrap.ok if bootstrap else None,
                "support_request_count": bootstrap.support_request_count if bootstrap else 0,
                "route_retry_count": bootstrap.route_retry_count if bootstrap else 0,
                "stop_reason": bootstrap.stop_reason if bootstrap else None,
                "error": bootstrap.error if bootstrap else None,
            },
            "rate_headers": self.api_manager.rate_limits.get("SearchTimeline", {}),
            "output_paths": {"raw_pages": page_output_paths},
        }
        outputs = self._save_exports(slug, product, raw_query, tweets, debug, metadata)
        successful_reasons = {
            "success_search_window_crossed",
            "success_reached_known_ground",
            "no_bottom_cursor",
            "repeated_cursor_history",
            "depth_one_complete",
        }
        status = "completed" if exhausted_reason in successful_reasons else ("partial" if tweets else "failed")
        endpoint_status = f"verified_{transport}" if status == "completed" else status
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
        
        state_key = self._state_key(search_def, product)
        current_state = self.search_state.get(state_key, {})
        is_success = status == "completed"
        
        new_state = {
            "last_status": exhausted_reason if is_success else f"error_http_{last_http_status}",
            "last_counts": report["counts"]
        }

        # High-water mark for the next run's early stop. Only ever advanced, and
        # only by a successful run: a partial run has not proven it saw the top
        # of the results, so trusting its newest tweet would leave a hole no
        # later run goes back for.
        seen_times = [value for value in (self._tweet_datetime(t) for t in tweets) if value]
        newest = max(seen_times) if seen_times else None
        previous_ground = self._parse_known_ground(current_state)
        if is_success and newest and (previous_ground is None or newest > previous_ground):
            new_state["newest_seen_at"] = newest.isoformat() + "Z"
        elif previous_ground:
            new_state["newest_seen_at"] = current_state.get("newest_seen_at")

        if is_success:
            new_state["last_checked_at"] = datetime.utcnow().isoformat() + "Z"
        else:
            new_state["last_checked_at"] = current_state.get("last_checked_at")
            
        self.search_state[state_key] = new_state
        self._save_json(self.state_file, self.search_state)
        self._save_json(self.reports_root / f"{slug}_{product.lower()}_{self.storage._batch_name()}.json", report)
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
                force_run=force_run,
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
    parser.add_argument("--once", action="store_true", help="Run one cycle instead of continuous mode.")
    parser.add_argument("--check-interval", type=int, default=60)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    monitor = SearchTimelineMonitor(
        config_path=args.config,
        search_config_path=args.search_config,
    )
    only = set(args.only or []) or None
    if args.once:
        reports = monitor.run_cycle(only_names=only)
        monitor._print_cycle_summary(reports, only)
    else:
        monitor.run_continuous(only_names=only, check_interval=args.check_interval)


if __name__ == "__main__":
    main()
