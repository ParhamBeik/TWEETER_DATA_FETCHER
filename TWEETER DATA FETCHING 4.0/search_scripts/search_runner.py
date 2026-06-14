#!/usr/bin/env python3
"""
V4 isolated Advanced SearchTimeline monitor.

Raw pages are stored as data/search/raw/{search_slug}/{product}/{jalali_batch}/page_{i}.json.
This script intentionally does not route through main_orchestrator.py.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote, urlencode

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.config.tier_config import DEFAULT_PRIORITY_POLICIES
from shared.core.fetcher_engine import FetcherEngine
from shared.core.set_operations import TweetSetProcessor
from shared.core.windowing import parse_twitter_timestamp
from shared.data_pipeline.storage_manager import StorageManager


VALID_PRODUCTS = {"Top", "Latest", "Media", "People"}


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


class SearchTimelineMonitor:
    """Continuous SearchTimeline monitor using v4 auth/rate-limit infrastructure."""

    def __init__(self, config_path: str = "shared/config/config.json", search_config_path: str = "shared/config/search_config.json"):
        self.project_root = Path(__file__).resolve().parents[1]
        self.fetcher = FetcherEngine(config_path=config_path, subsystem="search")
        self.api_manager = self.fetcher.api_manager
        self.config = self.api_manager.config
        self.storage = StorageManager(base_dir=self.project_root, subsystem="search")
        self.processor = TweetSetProcessor()
        self.search_defs = self._load_search_config(search_config_path)
        self.search_root = self.project_root / "data" / "search"
        self.raw_root = self.search_root / "raw"
        self.processed_root = self.search_root / "processed"
        self.debug_root = self.search_root / "debug"
        self.reports_root = self.search_root / "reports"
        self.state_file = self.search_root / "state" / "search_state.json"
        self.search_state = self._load_json(self.state_file, {})
        for path in [self.raw_root, self.processed_root, self.debug_root, self.reports_root, self.state_file.parent]:
            path.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, path: str) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.project_root / candidate

    def _load_search_config(self, path: str) -> List[Dict[str, Any]]:
        cfg_path = self._resolve_path(path)
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
        return {
            "poll_interval_seconds": int(search_def.get("poll_interval_seconds", defaults["poll_interval_seconds"])),
            "pagination_safety_cap_pages": int(
                search_def.get(
                    "pagination_safety_cap_pages",
                    self.config.get("api_config", {}).get("pagination_safety_cap_pages", 50),
                )
            ),
            "max_retries": int(search_def.get("max_retries", 3)),
            "rolling_hours": int(search_def.get("rolling_hours", 24)),
        }

    def _search_variables(self, search_def: Dict[str, Any], raw_query: str, product: str, cursor: Optional[str]) -> Dict[str, Any]:
        count = max(1, min(int(search_def.get("count", 20)), 100))
        variables = {
            "rawQuery": raw_query,
            "count": count,
            "querySource": str(search_def.get("query_source", "typed_query")),
            "product": product,
            "withGrokTranslatedBio": bool(search_def.get("with_grok_translated_bio", True)),
            "withQuickPromoteEligibilityTweetFields": bool(search_def.get("with_quick_promote_eligibility_tweet_fields", False)),
        }
        if cursor:
            variables["cursor"] = cursor
        for key, value in (search_def.get("variable_overrides", {}) or {}).items():
            variables[str(key)] = value
        return variables

    def _search_features(self, search_def: Dict[str, Any]) -> Dict[str, Any]:
        features = self.fetcher._timeline_features("SearchTimeline")
        for source in [self.config.get("search_timeline_feature_overrides", {}), search_def.get("feature_overrides", {})]:
            if isinstance(source, dict):
                features.update({str(k): v for k, v in source.items()})
        return features

    def _field_toggles(self, search_def: Dict[str, Any]) -> Dict[str, Any]:
        toggles = self.fetcher._timeline_field_toggles("SearchTimeline")
        overrides = search_def.get("field_toggle_overrides", {})
        if isinstance(overrides, dict):
            toggles.update({str(k): v for k, v in overrides.items()})
        return toggles

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
        return bool(dated and min(dated) <= window_start)

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
        if cursor and has_entries and new_items_count <= 0:
            return "no_new_tweets_on_cursor_page"
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
        search_def: Dict[str, Any],
        raw_query: str,
        product: str,
        search_url: str,
        cursor: Optional[str],
        retries: int,
        *,
        has_pages: bool = False,
    ) -> Dict[str, Any]:
        endpoint = "SearchTimeline"
        query_id = self.api_manager.get_query_id(endpoint)
        if not query_id:
            raise RuntimeError("Missing api_config.search_timeline_query_id")
        url = self.fetcher._build_graphql_url(
            endpoint=endpoint,
            query_id=query_id,
            variables=self._search_variables(search_def, raw_query, product, cursor),
            features=self._search_features(search_def),
            field_toggles=self._field_toggles(search_def),
        )
        retry_policy = self.api_manager.retry_policy()
        max_attempts = max(
            max(1, retries),
            int(retry_policy.get("client_error_attempts", self.fetcher.max_cursor_error_retries)),
            int(retry_policy.get("server_error_attempts", self.fetcher.max_cursor_error_retries)),
            int(retry_policy.get("request_error_attempts", self.fetcher.max_cursor_error_retries)),
        )
        errors: List[Dict[str, Any]] = []
        attempts = 0
        request_headers = {"referer": search_url, "x-twitter-active-user": "yes"}
        for attempt in range(max_attempts):
            attempts += 1
            try:
                response = self.api_manager.perform_get(
                    endpoint=endpoint,
                    url=url,
                    max_retries=1,
                    headers=request_headers,
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
                    client_attempts = int(retry_policy.get("client_error_attempts", self.fetcher.max_cursor_error_retries))
                    if attempt < client_attempts - 1:
                        self.api_manager.jitter_sleep(
                            float(retry_policy.get("client_error_min_seconds", 10)),
                            float(retry_policy.get("client_error_max_seconds", 20)),
                            reason=f"SearchTimeline HTTP {response.status_code} retry {attempt + 1}/{client_attempts}",
                        )
                        continue
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

    def should_fetch_search(self, search_def: Dict[str, Any], product: str, interval_seconds: int) -> bool:
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
        batch_dir = self._raw_batch_dir(slug, product)
        seen_ids: Set[str] = set()
        cursor: Optional[str] = None
        cursor_history: Set[str] = set()
        tweets: List[Dict[str, Any]] = []
        debug: Dict[str, Any] = {}
        attempts = 0
        error_samples: List[Dict[str, Any]] = []
        last_http_status: Optional[int] = None
        exhausted_reason = "unknown"
        rolling_hours = int(policy["rolling_hours"])
        window_start = datetime.utcnow() - timedelta(hours=max(1, rolling_hours))

        self.api_manager.warmup_url(search_url, timeout=int(self.config.get("api_config", {}).get("search_warmup_seconds", 30)))
        if self.fetcher.first_request_warmup_seconds > 0:
            time.sleep(self.fetcher.first_request_warmup_seconds)
        for page in range(1, int(policy["pagination_safety_cap_pages"]) + 1):
            payload = self._request_page(
                search_def,
                raw_query,
                product,
                search_url,
                cursor,
                int(policy["max_retries"]),
                has_pages=bool(tweets),
            )
            attempts += int(payload.get("_attempts", 0) or 0)
            last_http_status = payload.get("_status", last_http_status)
            error_samples.extend(payload.get("_error_samples", []) if isinstance(payload.get("_error_samples"), list) else [])
            if payload.get("_failure"):
                exhausted_reason = str(payload["_failure"])
                break
            payload.pop("_attempts", None)
            payload.pop("_error_samples", None)
            payload.pop("_status", None)
            self.storage.save_raw_page(batch_dir, page, payload)
            page_result = self._parse_search_page(payload, seen_ids, capture_debug=(page == 1))
            tweets.extend(page_result["tweets"])
            if page == 1:
                debug = {key: page_result.get(key) for key in ["entry_type_counts", "cursor_candidates", "skipped_entries", "processed_entries", "selected_cursor_source"]}
            next_cursor = page_result.get("next_cursor")
            if self._page_crossed_search_window(page_result["tweets"], window_start):
                exhausted_reason = "success_search_window_crossed"
                break
            stall_reason = self.classify_search_stall(
                cursor=cursor,
                next_cursor=next_cursor,
                has_entries=bool(page_result.get("has_entries")),
                new_items_count=len(page_result.get("tweets", [])),
                cursor_history=cursor_history,
            )
            if stall_reason:
                exhausted_reason = stall_reason
                break
            cursor_history.add(str(next_cursor))
            cursor = str(next_cursor)
            self.api_manager.human_delay("between_pages")
        else:
            exhausted_reason = "partial_safety_cap_reached"

        metadata = {
            "pages_requested": int(policy["pagination_safety_cap_pages"]),
            "pages_saved": len(list(batch_dir.glob("page_*.json"))),
            "exhausted_reason": exhausted_reason,
            "cursor_history": sorted(cursor_history),
            "raw_batch_path": str(batch_dir),
            "rolling_hours": rolling_hours,
            "window_start_utc": window_start.isoformat() + "Z",
            "attempts": attempts,
            "last_http_status": last_http_status,
            "error_samples": error_samples[-5:],
        }
        outputs = self._save_exports(slug, product, raw_query, tweets, debug, metadata)
        report = {"search": search_def.get("name", slug), "slug": slug, "product": product, "raw_query": raw_query, "metadata": metadata, "counts": {"tweets": len(tweets)}, "outputs": outputs}
        self.search_state[self._state_key(search_def, product)] = {"last_checked_at": datetime.utcnow().isoformat() + "Z", "last_status": exhausted_reason, "last_counts": report["counts"]}
        self._save_json(self.state_file, self.search_state)
        self._save_json(self.reports_root / f"{slug}_{product.lower()}_{self.storage._jalali_batch_name()}.json", report)
        return report

    def run_cycle(self, only_names: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
        reports = []
        for search_def in self.search_defs:
            if not search_def.get("enabled", True):
                continue
            name = str(search_def.get("name", ""))
            if only_names and name not in only_names and SearchQueryBuilder.slug(search_def) not in only_names:
                continue
            product = SearchQueryBuilder.normalize_product(str(search_def.get("product", "Top")))
            policy = self._policy_for_search(search_def)
            if not self.should_fetch_search(search_def, product, int(policy["poll_interval_seconds"])):
                continue
            reports.append(self.monitor_search(search_def))
        return reports

    def run_continuous(self, only_names: Optional[Set[str]] = None, check_interval: int = 60) -> None:
        print("Starting v4 SearchTimeline monitor. Press Ctrl+C to stop.")
        while True:
            reports = self.run_cycle(only_names=only_names)
            print(f"Search cycle complete: {len(reports)} search(es) fetched")
            time.sleep(max(1, check_interval))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run isolated v4 SearchTimeline monitoring.")
    parser.add_argument("--config", default="shared/config/config.json")
    parser.add_argument("--search-config", default="shared/config/search_config.json")
    parser.add_argument("--only", action="append", help="Limit to search name/slug; can be repeated.")
    parser.add_argument("--once", action="store_true", help="Run one validation cycle instead of continuous mode.")
    parser.add_argument("--check-interval", type=int, default=60)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    monitor = SearchTimelineMonitor(config_path=args.config, search_config_path=args.search_config)
    only = set(args.only or []) or None
    if args.once:
        print(monitor.run_cycle(only_names=only))
    else:
        monitor.run_continuous(only_names=only, check_interval=args.check_interval)


if __name__ == "__main__":
    main()
