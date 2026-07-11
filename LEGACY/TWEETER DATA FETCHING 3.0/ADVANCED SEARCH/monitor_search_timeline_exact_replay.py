#!/usr/bin/env python3
"""
Twitter SearchTimeline Exact Replay Monitor

Deterministic replay model:
- One stable requests.Session
- Frozen SearchTimeline features payload
- Cursor-only pagination continuation
- No rolling-window filtering

Reuses existing architecture:
- api_manager.py
- storage_manager.py
- fetch_historical_tweets_hybrid.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import quote, urlencode

from debug_logger import setup_logging, dump_request


def _maybe_reexec_project_venv():
    script_path = Path(__file__).resolve()
    current_python = Path(sys.executable).resolve()
    current_prefix = Path(sys.prefix).resolve()
    candidates = [
        script_path.parent / ".venv" / "bin" / "python",
        script_path.parent.parent / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            target = candidate.resolve()
            target_prefix = candidate.parent.parent.resolve()
            if target != current_python or target_prefix != current_prefix:
                os.execv(str(target), [str(target), str(script_path), *sys.argv[1:]])
            return


_maybe_reexec_project_venv()

from api_manager import APIManager
from fetch_historical_tweets_hybrid import TwitterHistoricalFetcher
from storage_manager import StorageManager

try:
    import pytz
except ImportError:
    print(f"ERROR: Missing dependency in interpreter {sys.executable}")
    print("Run: python -m pip install pytz")
    sys.exit(1)


SEP = "═" * 70
VALID_PRODUCTS = {"Top", "Latest", "Media", "People"}

# ============================================================
# EDIT THESE DEFAULTS HERE (simple run style)
# ============================================================
DEFAULT_CONFIG_PATH = "config.json"
DEFAULT_SEARCH_CONFIG_PATH = "search_config.json"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_MAX_PAGES = 50
DEFAULT_MAX_TWEETS_TARGET = 500
DEFAULT_COUNT_PER_PAGE = 20  # CRITICAL: Must match browser behavior for cursor validity
ENABLE_SINGLE_WARMUP = True
DEFAULT_AUTO_GENERATE_RAW_QUERY = True
DEFAULT_AUTO_GENERATE_NAME = True
DEFAULT_CONTINUITY_WARMUP_MODE = "search_page_once"  # off | search_page_once
# CRITICAL: Browser naturally loads search page before GraphQL requests
# This warmup mimics that behavior and establishes session context for cursor validity
STRICT_BOTTOM_CURSOR_ONLY = False
EMIT_VERBOSE_CONTINUITY_DIFF = True

OUTPUT_JSON_DIR_NAME = "JSON_FILES"
OUTPUT_DEBUG_DIR_NAME = "DEBUGGING"

# Frozen from your successful browser captures
FROZEN_SEARCH_FEATURES: Dict[str, object] = {
    "articles_preview_enabled": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_inline_media_enabled": False,
    "longform_notetweets_rich_text_read_enabled": True,
    "post_ctas_fetch_enabled": True,
    "premium_content_api_read_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_grok_analysis_button_from_backend": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_profile_redirect_enabled": False,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "rweb_cashtags_composer_attachment_enabled": True,
    "rweb_cashtags_enabled": True,
    "rweb_conversational_replies_downvote_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "rweb_video_screen_enabled": False,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "verified_phone_label_enabled": False,
    "view_counts_everywhere_api_enabled": True,
}


class SearchQueryBuilder:
    @staticmethod
    def _sanitize(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    @staticmethod
    def _normalize_product(value: str) -> str:
        candidate = str(value or "Top").strip().title()
        return candidate if candidate in VALID_PRODUCTS else "Top"

    @staticmethod
    def _clean_keyword_tokens(values: List[str]) -> List[str]:
        tokens: List[str] = []
        for value in values or []:
            clean = SearchQueryBuilder._sanitize(value)
            if clean:
                tokens.append(clean)
        return tokens

    @staticmethod
    def build_dynamic_name(search_def: Dict) -> str:
        tokens = SearchQueryBuilder._clean_keyword_tokens(search_def.get("include_keywords", []))
        if tokens:
            base = "_".join(tokens[:8])
        else:
            fallback = SearchQueryBuilder._sanitize(search_def.get("exact_query", "")) or SearchQueryBuilder._sanitize(
                search_def.get("raw_query", "")
            )
            base = fallback if fallback else "search_timeline"
        slug = re.sub(r"[^a-zA-Z0-9_]+", "_", base).strip("_")
        slug = re.sub(r"_+", "_", slug)
        return slug or "search_timeline"

    @staticmethod
    def build_raw_query(search_def: Dict, now_dt: Optional[datetime] = None, force_dynamic: bool = False) -> str:
        if not force_dynamic:
            if search_def.get("raw_query"):
                return str(search_def["raw_query"]).strip()

            if bool(search_def.get("preserve_exact_query", False)):
                explicit = str(search_def.get("exact_query") or "").strip()
                if explicit:
                    return explicit

        parts: List[str] = []
        include_keywords = [
            SearchQueryBuilder._sanitize(term)
            for term in search_def.get("include_keywords", [])
            if SearchQueryBuilder._sanitize(term)
        ]
        if include_keywords:
            parts.append(include_keywords[0] if len(include_keywords) == 1 else "(" + " OR ".join(include_keywords) + ")")

        for phrase in search_def.get("exact_phrases", []):
            clean = SearchQueryBuilder._sanitize(phrase).replace('"', "")
            if clean:
                parts.append(clean)

        for key in ["exclude_keywords", "from_accounts", "to_accounts", "mentions"]:
            values = search_def.get(key, [])
            for item in values:
                clean = SearchQueryBuilder._sanitize(item)
                if not clean:
                    continue
                if key == "exclude_keywords":
                    parts.append(f"-{clean}")
                elif key == "from_accounts":
                    parts.append(f"from:{clean.lstrip('@')}")
                elif key == "to_accounts":
                    parts.append(f"to:{clean.lstrip('@')}")
                else:
                    parts.append(f"@{clean.lstrip('@')}")

        lang = SearchQueryBuilder._sanitize(search_def.get("lang", ""))
        if lang:
            parts.append(f"lang:{lang}")

        for metric in ["min_replies", "min_faves", "min_retweets"]:
            value = search_def.get(metric)
            if value is not None and str(value).strip():
                parts.append(f"{metric}:{int(value)}")

        since = SearchQueryBuilder._sanitize(search_def.get("since", ""))
        until = SearchQueryBuilder._sanitize(search_def.get("until", ""))
        since_days = search_def.get("since_days")

        if not since and since_days is not None:
            base_now = now_dt or datetime.now()
            try:
                since = (base_now - timedelta(days=int(since_days))).date().isoformat()
            except Exception:
                since = ""

        if since:
            parts.append(f"since:{since}")
        if until:
            parts.append(f"until:{until}")

        for extra in search_def.get("extra_filters", []):
            clean = SearchQueryBuilder._sanitize(extra)
            if clean:
                parts.append(clean)

        return " ".join(parts).strip()

    @staticmethod
    def build_search_url(raw_query: str, product: str) -> str:
        normalized_product = SearchQueryBuilder._normalize_product(product)
        encoded_query = quote(raw_query, safe="()")
        url = f"https://x.com/search?q={encoded_query}"
        filter_map = {"Top": "top", "Latest": "live", "Media": "media", "People": "user"}
        f = filter_map.get(normalized_product)
        if f:
            url += f"&f={f}"
        url += "&src=typed_query"
        return url


class SearchTimelineExactReplayMonitor:
    def __init__(
        self,
        config_path: str = DEFAULT_CONFIG_PATH,
        search_config_path: str = DEFAULT_SEARCH_CONFIG_PATH,
        dry_run: bool = False,
    ):
        self.base_dir = Path(__file__).parent
        self.config_path = str(self._resolve_path(config_path))
        self.search_config_path = str(self._resolve_path(search_config_path))
        self.dry_run = dry_run

        self.api_manager = APIManager(self.config_path, state_dir=self.base_dir / "data" / "STATE")
        self.storage_manager = StorageManager(self.base_dir, timezone="Asia/Tehran")
        self.fetcher = TwitterHistoricalFetcher(self.config_path)
        self.fetcher.api_manager = self.api_manager
        self.fetcher.storage_manager = self.storage_manager

        self.config = self.api_manager.config
        self.tz = pytz.timezone("Asia/Tehran")
        self.search_definitions = self._load_search_config(self.search_config_path)

    def _resolve_path(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.base_dir / candidate
        return candidate

    def _load_search_config(self, path: str) -> List[Dict]:
        cfg = Path(path)
        if not cfg.exists():
            raise FileNotFoundError(f"Search config not found: {cfg}")
        with open(cfg, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, list):
            raise ValueError("search_config.json must be a JSON list")
        return [entry for entry in payload if isinstance(entry, dict)]

    def _sync_search_config_derived_fields(self) -> bool:
        """
        Keep search_config.json consistent with derived runtime values by writing:
        - name (when auto_generate_name=true)
        - raw_query (when auto_generate_raw_query=true)
        """
        cfg = Path(self.search_config_path)
        if not cfg.exists():
            return False

        with open(cfg, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, list):
            return False

        changed = False
        now_dt = datetime.now(self.tz)
        for entry in payload:
            if not isinstance(entry, dict):
                continue

            if bool(entry.get("auto_generate_name", DEFAULT_AUTO_GENERATE_NAME)):
                dynamic_name = SearchQueryBuilder.build_dynamic_name(entry)
                if str(entry.get("name", "")).strip() != dynamic_name:
                    entry["name"] = dynamic_name
                    changed = True

            if bool(entry.get("auto_generate_raw_query", DEFAULT_AUTO_GENERATE_RAW_QUERY)):
                dynamic_query = SearchQueryBuilder.build_raw_query(entry, now_dt=now_dt, force_dynamic=True)
                if str(entry.get("raw_query", "")).strip() != dynamic_query:
                    entry["raw_query"] = dynamic_query
                    changed = True

        if changed:
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")

        return changed

    def _compact_json(self, payload: Dict) -> str:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    def _stable_json(self, payload: Dict) -> str:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True)

    def _fingerprint(self, payload: Dict) -> str:
        return hashlib.sha256(self._stable_json(payload).encode("utf-8")).hexdigest()

    def _cookie_snapshot(self) -> Dict[str, str]:
        return {str(cookie.name): str(cookie.value) for cookie in self.api_manager.session.cookies if cookie.name}

    def _mask_tx_id(self, tx_id: str) -> str:
        tx = str(tx_id or "").strip()
        if len(tx) <= 20:
            return tx
        return f"{tx[:12]}...{tx[-8:]}"

    def _effective_name(self, search_def: Dict) -> str:
        auto_name = bool(search_def.get("auto_generate_name", DEFAULT_AUTO_GENERATE_NAME))
        if auto_name:
            return SearchQueryBuilder.build_dynamic_name(search_def)
        configured = str(search_def.get("name", "")).strip()
        return configured or SearchQueryBuilder.build_dynamic_name(search_def)

    def _effective_raw_query(self, search_def: Dict, now_dt: Optional[datetime] = None) -> str:
        auto_query = bool(search_def.get("auto_generate_raw_query", DEFAULT_AUTO_GENERATE_RAW_QUERY))
        if auto_query:
            return SearchQueryBuilder.build_raw_query(search_def, now_dt=now_dt, force_dynamic=True)
        return SearchQueryBuilder.build_raw_query(search_def, now_dt=now_dt, force_dynamic=False)

    def _search_paths(self, search_name: str, product: str) -> Dict[str, Path]:
        safe_name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", search_name).strip("_") or "search_timeline"

        root_dir = self.storage_manager.search_timeline_dir / safe_name / product
        json_dir = root_dir / OUTPUT_JSON_DIR_NAME
        debug_dir = root_dir / OUTPUT_DEBUG_DIR_NAME

        root_dir.mkdir(parents=True, exist_ok=True)
        json_dir.mkdir(parents=True, exist_ok=True)
        debug_dir.mkdir(parents=True, exist_ok=True)

        return {
            "root": root_dir,
            "json_dir": json_dir,
            "debug_dir": debug_dir,
            "safe_name": safe_name,
        }

    def _tweet_datetime(self, tweet: Dict) -> Optional[datetime]:
        raw = tweet.get("raw_timestamp")
        if isinstance(raw, str) and raw.strip():
            try:
                return datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y").astimezone(self.tz)
            except Exception:
                return None
        return None

    def _tweet_sort_key(self, tweet: Dict) -> float:
        dt = self._tweet_datetime(tweet)
        return dt.timestamp() if dt else 0.0

    def _extract_media_from_legacy(self, legacy: Dict) -> Dict[str, List[str]]:
        image_urls: Set[str] = set()
        video_urls: Set[str] = set()
        m3u8_urls: Set[str] = set()

        media_entities = legacy.get("extended_entities", {}).get("media", [])
        if not media_entities:
            media_entities = legacy.get("entities", {}).get("media", [])

        for media_item in media_entities:
            media_type = str(media_item.get("type", "")).lower()
            media_url = media_item.get("media_url_https") or media_item.get("media_url")
            if media_url:
                image_urls.add(str(media_url))

            if media_type in {"video", "animated_gif"}:
                variants = media_item.get("video_info", {}).get("variants", [])
                for variant in variants:
                    variant_url = str(variant.get("url", "")).strip()
                    if not variant_url:
                        continue
                    content_type = str(variant.get("content_type", "")).lower()
                    if ".m3u8" in variant_url or "mpegurl" in content_type:
                        m3u8_urls.add(variant_url)
                    else:
                        video_urls.add(variant_url)

        return {
            "image_urls": sorted(image_urls),
            "video_urls": sorted(video_urls),
            "m3u8_urls": sorted(m3u8_urls),
        }

    def _extract_card_links(self, tweet_obj: Dict) -> Dict[str, List[str]]:
        card_urls: Set[str] = set()
        card_image_urls: Set[str] = set()
        external_urls: Set[str] = set()

        bindings = tweet_obj.get("card", {}).get("legacy", {}).get("binding_values", [])
        for binding in bindings:
            key = str(binding.get("key", "")).strip()
            value = binding.get("value", {})
            if not isinstance(value, dict):
                continue

            string_value = str(value.get("string_value", "")).strip()
            image_value = value.get("image_value", {}) if isinstance(value.get("image_value"), dict) else {}
            image_url = str(image_value.get("url", "")).strip()

            if image_url.startswith("http"):
                card_image_urls.add(image_url)
            if string_value.startswith("http"):
                if "card_url" in key or key in {"card_url", "player_url"}:
                    card_urls.add(string_value)
                external_urls.add(string_value)

        return {
            "card_urls": sorted(card_urls),
            "card_image_urls": sorted(card_image_urls),
            "external_urls": sorted(external_urls),
        }

    def _enrich_search_tweet(self, parsed: Dict, tweet_obj: Dict) -> Dict:
        legacy = tweet_obj.get("legacy", {}) if isinstance(tweet_obj, dict) else {}
        own_media = self._extract_media_from_legacy(legacy if isinstance(legacy, dict) else {})
        card_links = self._extract_card_links(tweet_obj)

        quoted_image_urls: Set[str] = set()
        quoted_video_urls: Set[str] = set()
        quoted_m3u8_urls: Set[str] = set()

        quoted_wrapper = tweet_obj.get("quoted_status_result", {})
        quoted_obj = self.fetcher._unwrap_tweet_result(quoted_wrapper) if isinstance(quoted_wrapper, dict) else None
        if isinstance(quoted_obj, dict):
            quoted_media = self._extract_media_from_legacy(quoted_obj.get("legacy", {}))
            quoted_image_urls.update(quoted_media["image_urls"])
            quoted_video_urls.update(quoted_media["video_urls"])
            quoted_m3u8_urls.update(quoted_media["m3u8_urls"])

            quoted_card = self._extract_card_links(quoted_obj)
            card_links["card_urls"].extend(quoted_card["card_urls"])
            card_links["card_image_urls"].extend(quoted_card["card_image_urls"])
            card_links["external_urls"].extend(quoted_card["external_urls"])

        entities = parsed.get("entities", {}) if isinstance(parsed.get("entities"), dict) else {}
        entity_urls: Set[str] = set()
        for url_entry in entities.get("urls", []):
            if isinstance(url_entry, dict):
                expanded = str(url_entry.get("expanded") or url_entry.get("short") or "").strip()
                if expanded.startswith("http"):
                    entity_urls.add(expanded)

        parsed["media"] = {
            "tweet_urls": [parsed.get("url")] if parsed.get("url") else [],
            "image_urls": own_media["image_urls"],
            "video_urls": own_media["video_urls"],
            "m3u8_urls": own_media["m3u8_urls"],
            "quoted_image_urls": sorted(quoted_image_urls),
            "quoted_video_urls": sorted(quoted_video_urls),
            "quoted_m3u8_urls": sorted(quoted_m3u8_urls),
            "card_urls": sorted(set(card_links["card_urls"])),
            "card_image_urls": sorted(set(card_links["card_image_urls"])),
            "external_urls": sorted(set(card_links["external_urls"]) | entity_urls),
        }
        return parsed

    def _extract_instructions(self, response_json: Dict) -> List[Dict]:
        return (
            response_json.get("data", {})
            .get("search_by_raw_query", {})
            .get("search_timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )

    def _collect_cursor_candidates(self, entry: Dict, source_path: str) -> List[Dict]:
        candidates: List[Dict] = []
        if not isinstance(entry, dict):
            return candidates

        content = entry.get("content", {})
        entry_id = str(entry.get("entryId", ""))
        if not isinstance(content, dict):
            return candidates

        typename = str(content.get("__typename", ""))
        cursor_type = str(content.get("cursorType", ""))
        value = content.get("value")
        if value:
            value_s = str(value)
            is_bottom = cursor_type.lower() == "bottom" or entry_id.startswith("cursor-bottom-")
            score = 100 if is_bottom else (70 if "cursor" in entry_id.lower() else 40)
            candidates.append(
                {
                    "value": value_s,
                    "source_path": source_path,
                    "entry_id": entry_id,
                    "typename": typename,
                    "cursor_type": cursor_type,
                    "is_bottom": is_bottom,
                    "score": score,
                }
            )

        items = content.get("items", [])
        if isinstance(items, list):
            for idx, item_entry in enumerate(items):
                if not isinstance(item_entry, dict):
                    continue
                nested_item = item_entry.get("item", {})
                if not isinstance(nested_item, dict):
                    continue
                nested_content = nested_item.get("content", {})
                if not isinstance(nested_content, dict):
                    continue
                nested_value = nested_content.get("value")
                if not nested_value:
                    continue
                nested_cursor_type = str(nested_content.get("cursorType", ""))
                nested_typename = str(nested_content.get("__typename", ""))
                nested_is_bottom = nested_cursor_type.lower() == "bottom"
                candidates.append(
                    {
                        "value": str(nested_value),
                        "source_path": f"{source_path}.items[{idx}].item.content",
                        "entry_id": entry_id,
                        "typename": nested_typename,
                        "cursor_type": nested_cursor_type,
                        "is_bottom": nested_is_bottom,
                        "score": 95 if nested_is_bottom else 35,
                    }
                )

        return candidates

    def _parse_page(self, instructions: List[Dict], seen_ids: Set[str]) -> Dict:
        tweets: List[Dict] = []
        cursor_candidates: List[Dict] = []
        entry_type_counts: Dict[str, int] = {}
        skipped_entries: List[Dict] = []
        duplicate_count = 0

        def bump(key: str):
            entry_type_counts[key] = int(entry_type_counts.get(key, 0)) + 1

        def skip(entry_id: str, typename: str, reason: str):
            skipped_entries.append({"entry_id": entry_id or "unknown", "typename": typename or "unknown", "reason": reason})

        def try_add_tweet(tweet_wrapper: Dict, entry_id: str, typename: str, source: str):
            nonlocal duplicate_count
            tweet_obj = self.fetcher._unwrap_tweet_result(tweet_wrapper)
            if not tweet_obj:
                skip(entry_id, typename, f"unwrap_failed:{source}")
                return
            parsed = self.fetcher._parse_tweet(tweet_obj)
            if not parsed:
                skip(entry_id, typename, f"parse_failed:{source}")
                return
            parsed = self._enrich_search_tweet(parsed, tweet_obj)
            tweet_id = str(parsed.get("id") or "").strip()
            if not tweet_id:
                skip(entry_id, typename, f"missing_tweet_id:{source}")
                return
            if tweet_id in seen_ids:
                duplicate_count += 1
                return
            seen_ids.add(tweet_id)
            tweets.append(parsed)

        for inst in instructions:
            inst_type = str(inst.get("type", ""))
            bump(f"instruction:{inst_type or 'unknown'}")

            if inst_type == "TimelineReplaceEntry":
                entry = inst.get("entry", {})
                if isinstance(entry, dict):
                    cursor_candidates.extend(self._collect_cursor_candidates(entry, "TimelineReplaceEntry.entry"))
                continue

            if inst_type == "TimelinePinEntry":
                entry = inst.get("entry", {})
                if not isinstance(entry, dict):
                    continue
                cursor_candidates.extend(self._collect_cursor_candidates(entry, "TimelinePinEntry.entry"))
                entry_id = str(entry.get("entryId", ""))
                content = entry.get("content", {})
                typename = str(content.get("__typename", "")) if isinstance(content, dict) else ""
                item_content = content.get("itemContent", {}) if isinstance(content, dict) else {}
                if isinstance(item_content, dict):
                    try_add_tweet(
                        item_content.get("tweet_results", {}),
                        entry_id,
                        typename,
                        "TimelinePinEntry.entry.content.itemContent.tweet_results",
                    )
                continue

            if inst_type != "TimelineAddEntries":
                continue

            for entry in inst.get("entries", []):
                if not isinstance(entry, dict):
                    continue
                cursor_candidates.extend(self._collect_cursor_candidates(entry, "TimelineAddEntries.entries"))

                entry_id = str(entry.get("entryId", ""))
                content = entry.get("content", {})
                typename = str(content.get("__typename", "")) if isinstance(content, dict) else "unknown"
                bump(f"entry:{typename or 'unknown'}")

                if isinstance(content, dict):
                    item_content = content.get("itemContent", {})
                    # BUG FIX: Recognize tweets in all entry shapes, not just "tweet-" prefix
                    # Later pages may use "homeConversation-", "profile-conversation-", etc.
                    # Check if itemContent has tweet_results, which is the reliable indicator
                    has_tweet_results = isinstance(item_content, dict) and "tweet_results" in item_content
                    if has_tweet_results or entry_id.startswith("tweet-") or typename == "TimelineTimelineItem":
                        try_add_tweet(
                            item_content.get("tweet_results", {}),
                            entry_id,
                            typename,
                            "TimelineAddEntries.entries[].content.itemContent.tweet_results",
                        )
                        continue

                    items = content.get("items", [])
                    if isinstance(items, list):
                        for idx, item_entry in enumerate(items):
                            if not isinstance(item_entry, dict):
                                continue
                            item = item_entry.get("item", {})
                            if not isinstance(item, dict):
                                continue
                            module_item_content = item.get("itemContent", {})
                            module_typename = str(item.get("content", {}).get("__typename", "")) if isinstance(item.get("content"), dict) else typename
                            module_entry_id = f"{entry_id}#item{idx}"
                            if not isinstance(module_item_content, dict):
                                skip(module_entry_id, module_typename, "module_item_without_itemContent")
                                continue
                            tweet_results = module_item_content.get("tweet_results", {})
                            if not tweet_results:
                                skip(module_entry_id, module_typename, "module_item_without_tweet_results")
                                continue
                            try_add_tweet(
                                tweet_results,
                                module_entry_id,
                                module_typename,
                                "TimelineAddEntries.entries[].content.items[].item.itemContent.tweet_results",
                            )
                        continue

                    if entry_id.startswith("promoted-") or "promoted" in entry_id.lower():
                        skip(entry_id, typename, "promoted_entry")
                    elif "cursor" not in entry_id.lower():
                        skip(entry_id, typename, "unsupported_entry_shape")

        next_cursor = None
        selected_cursor_source = None
        if cursor_candidates:
            scored = sorted(cursor_candidates, key=lambda c: int(c.get("score", 0)), reverse=True)
            if STRICT_BOTTOM_CURSOR_ONLY:
                bottom_only = [c for c in scored if bool(c.get("is_bottom"))]
                best = bottom_only[0] if bottom_only else scored[0]
            else:
                best = scored[0]
            cursor_candidates = scored
            next_cursor = str(best.get("value", "")).strip() or None
            selected_cursor_source = str(best.get("source_path", "")) if next_cursor else None

        return {
            "tweets": tweets,
            "next_cursor": next_cursor,
            "cursor_candidates": cursor_candidates,
            "selected_cursor_source": selected_cursor_source,
            "entry_type_counts": entry_type_counts,
            "skipped_entries": skipped_entries,
            "duplicate_count": duplicate_count,
        }

    def _build_base_variables(self, search_def: Dict, raw_query: str, product: str) -> Dict:
        count = int(search_def.get("count", DEFAULT_COUNT_PER_PAGE))
        # CRITICAL: X.com SearchTimeline cursors are tied to the count value used on page 1
        # Using count > 20 causes cursor rejection (404) on subsequent pages
        # Browser testing shows count=20 works reliably for multi-page pagination
        count = max(1, min(count, 20))  # Cap at 20, not 100
        return {
            "rawQuery": raw_query,
            "count": count,
            "querySource": str(search_def.get("query_source", "typed_query")),
            "product": product,
            "withGrokTranslatedBio": bool(search_def.get("with_grok_translated_bio", True)),
            "withQuickPromoteEligibilityTweetFields": bool(search_def.get("with_quick_promote_eligibility_tweet_fields", False)),
        }

    def _build_frozen_headers(self, search_url: str) -> Dict[str, str]:
        headers = dict(self.api_manager.session.headers)
        headers["referer"] = search_url
        headers["x-twitter-active-user"] = "yes"
        return {str(k): str(v) for k, v in headers.items() if v is not None}

    def _save_page_json(self, json_dir: Path, safe_name: str, payload: Dict) -> Path:
        page_no = int(payload.get("page", 0))
        output = json_dir / f"{safe_name.upper()}__raw_page_{page_no:03d}.json"
        with open(output, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return output

    def _save_first_page_debug(self, debug_dir: Path, safe_name: str, parser_debug: Dict):
        with open(debug_dir / f"{safe_name.upper()}__debug_first_page_entry_types.json", "w", encoding="utf-8") as f:
            json.dump(parser_debug.get("entry_type_counts", {}), f, ensure_ascii=False, indent=2)
        with open(debug_dir / f"{safe_name.upper()}__debug_first_page_cursor_candidates.json", "w", encoding="utf-8") as f:
            json.dump(parser_debug.get("cursor_candidates", []), f, ensure_ascii=False, indent=2)
        with open(debug_dir / f"{safe_name.upper()}__debug_first_page_skipped_entries.json", "w", encoding="utf-8") as f:
            json.dump(parser_debug.get("skipped_entries", []), f, ensure_ascii=False, indent=2)

    def _write_continuity_diagnostic(
        self,
        *,
        json_dir: Path,
        safe_name: str,
        search_name: str,
        stop_reason: str,
        query_id: str,
        tx_id: str,
        page_payloads: List[Dict],
    ) -> Dict:
        continuity_output = json_dir / f"{safe_name.upper()}__continuity_diagnostic.json"
        page1 = page_payloads[0] if len(page_payloads) >= 1 else {}
        page2 = page_payloads[1] if len(page_payloads) >= 2 else {}

        headers1 = page1.get("headers", {}) if isinstance(page1.get("headers"), dict) else {}
        headers2 = page2.get("headers", {}) if isinstance(page2.get("headers"), dict) else {}
        cookies1 = page1.get("cookies", {}) if isinstance(page1.get("cookies"), dict) else {}
        cookies2 = page2.get("cookies", {}) if isinstance(page2.get("cookies"), dict) else {}
        vars1 = page1.get("variables", {}) if isinstance(page1.get("variables"), dict) else {}
        vars2 = page2.get("variables", {}) if isinstance(page2.get("variables"), dict) else {}
        features1 = page1.get("features", {}) if isinstance(page1.get("features"), dict) else {}
        features2 = page2.get("features", {}) if isinstance(page2.get("features"), dict) else {}

        def diff_keys(a: Dict, b: Dict) -> List[str]:
            keys = sorted(set(a.keys()) | set(b.keys()))
            return [k for k in keys if a.get(k) != b.get(k)]

        header_diff = diff_keys(headers1, headers2)
        cookie_diff = diff_keys(cookies1, cookies2)
        variable_diff = diff_keys(vars1, vars2)
        non_cursor_variable_diff = [k for k in variable_diff if k != "cursor"]
        feature_diff = diff_keys(features1, features2)

        cursor_in_page2 = vars2.get("cursor")
        cursor_expected = page1.get("next_cursor")
        cursor_chain_ok = bool(cursor_in_page2 and cursor_expected and cursor_in_page2 == cursor_expected)

        continuity_signals = {
            "page1_status": page1.get("status"),
            "page2_status": page2.get("status"),
            "cursor_chain_ok": cursor_chain_ok,
            "header_diff_count": len(header_diff),
            "cookie_diff_count": len(cookie_diff),
            "variable_diff_count": len(variable_diff),
            "non_cursor_variable_diff_count": len(non_cursor_variable_diff),
            "feature_diff_count": len(feature_diff),
            "warmup_status": page1.get("warmup_status"),
        }

        probable_break_causes: List[str] = []
        if not cursor_chain_ok and len(page_payloads) >= 2:
            probable_break_causes.append("cursor_mismatch_between_page1_next_and_page2_cursor")
        if len(header_diff) > 0:
            probable_break_causes.append("request_headers_changed_between_pages")
        if len(cookie_diff) > 0:
            probable_break_causes.append("session_cookies_changed_between_pages")
        if len(feature_diff) > 0:
            probable_break_causes.append("features_payload_changed_between_pages")
        if len(non_cursor_variable_diff) > 0:
            probable_break_causes.append("non_cursor_variables_changed_between_pages")
        if len(page_payloads) >= 2 and page1.get("status") == 200 and page2.get("status") == 404 and cursor_chain_ok and len(header_diff) == 0 and len(cookie_diff) == 0 and len(feature_diff) == 0:
            probable_break_causes.append("server_side_context_rejection_with_identical_client_payload_shape")

        continuity_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "headers": headers1,
                    "features": features1,
                    "query_id": query_id,
                    "tx_id": tx_id,
                    "raw_query": vars1.get("rawQuery"),
                    "product": vars1.get("product"),
                    "count": vars1.get("count"),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

        diagnostic_payload = {
            "generated_at": datetime.now(self.tz).isoformat(),
            "search_name": search_name,
            "stop_reason": stop_reason,
            "query_id": query_id,
            "tx_id_masked": self._mask_tx_id(tx_id),
            "continuity_fingerprint": continuity_fingerprint,
            "signals": continuity_signals,
            "differences": {
                "header_diff_keys": header_diff,
                "cookie_diff_keys": cookie_diff,
                "variable_diff_keys": variable_diff,
                "non_cursor_variable_diff_keys": non_cursor_variable_diff,
                "feature_diff_keys": feature_diff,
            },
            "fingerprints": {
                "headers_page1": self._fingerprint(headers1) if headers1 else "",
                "headers_page2": self._fingerprint(headers2) if headers2 else "",
                "cookies_page1": self._fingerprint(cookies1) if cookies1 else "",
                "cookies_page2": self._fingerprint(cookies2) if cookies2 else "",
                "features_page1": self._fingerprint(features1) if features1 else "",
                "features_page2": self._fingerprint(features2) if features2 else "",
                "variables_page1": self._fingerprint(vars1) if vars1 else "",
                "variables_page2": self._fingerprint(vars2) if vars2 else "",
                "variables_no_cursor_page1": self._fingerprint({k: v for k, v in vars1.items() if k != "cursor"}) if vars1 else "",
                "variables_no_cursor_page2": self._fingerprint({k: v for k, v in vars2.items() if k != "cursor"}) if vars2 else "",
            },
            "cursor_chain": {
                "page1_next_cursor": cursor_expected,
                "page2_cursor": cursor_in_page2,
                "match": cursor_chain_ok,
            },
            "probable_break_causes": probable_break_causes,
            "pages": [
                {
                    "page": p.get("page"),
                    "status": p.get("status"),
                    "duration_ms": p.get("duration_ms"),
                    "tweet_count": p.get("tweet_count"),
                    "cursor": p.get("cursor"),
                    "next_cursor": p.get("next_cursor"),
                    "request_exception": p.get("request_exception"),
                }
                for p in page_payloads
            ],
        }

        with open(continuity_output, "w", encoding="utf-8") as f:
            json.dump(diagnostic_payload, f, ensure_ascii=False, indent=2)

        return {
            "path": continuity_output,
            "signals": continuity_signals,
            "probable_break_causes": probable_break_causes,
            "fingerprints": diagnostic_payload.get("fingerprints", {}),
        }

    def _persist_outputs(
        self,
        *,
        search_name: str,
        product: str,
        raw_query: str,
        search_url: str,
        query_id: str,
        tx_id: str,
        paths: Dict[str, Path],
        all_tweets: List[Dict],
        pages_completed: int,
        cursor_history: List[str],
        stop_reason: str,
        per_page_status: List[Dict],
    ) -> Dict:
        dedup_map: Dict[str, Dict] = {}
        duplicate_overwrites = 0
        for tweet in all_tweets:
            tweet_id = str(tweet.get("id") or "").strip()
            if not tweet_id:
                continue
            if tweet_id in dedup_map:
                duplicate_overwrites += 1
            dedup_map[tweet_id] = tweet
            self.storage_manager.register_tweet(
                tweet_id=tweet_id,
                account=str(tweet.get("account", "unknown")),
                stored_in=[f"SEARCH_TIMELINE/{search_name}/{product}"],
            )

        tweets_sorted = sorted(dedup_map.values(), key=self._tweet_sort_key, reverse=True)

        output_txt = paths["root"] / f"{paths['safe_name'].upper()}.txt"
        output_json = paths["json_dir"] / f"{paths['safe_name'].upper()}.json"

        payload = {
            "generated_at": datetime.now(self.tz).isoformat(),
            "search_name": search_name,
            "product": product,
            "raw_query": raw_query,
            "search_url": search_url,
            "query_id": query_id,
            "tx_id": self._mask_tx_id(tx_id),
            "counts": {
                "tweets_extracted_total": len(all_tweets),
                "tweets_saved_unique": len(tweets_sorted),
                "duplicate_overwrites": duplicate_overwrites,
            },
            "pagination": {
                "pages_completed": pages_completed,
                "stop_reason": stop_reason,
                "cursor_history": cursor_history,
                "per_page_status": per_page_status,
            },
            "tweets": tweets_sorted,
        }

        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        txt_body = self.storage_manager._format_tweets(tweets_sorted) if tweets_sorted else "No tweets extracted in this run."
        with open(output_txt, "w", encoding="utf-8") as f:
            f.write(txt_body)

        newest_dt = self._tweet_datetime(tweets_sorted[0]) if tweets_sorted else None
        oldest_dt = self._tweet_datetime(tweets_sorted[-1]) if tweets_sorted else None

        return {
            "output_txt": output_txt,
            "output_json": output_json,
            "saved_unique": len(tweets_sorted),
            "duplicate_overwrites": duplicate_overwrites,
            "newest_dt": newest_dt,
            "oldest_dt": oldest_dt,
        }

    def _selected_searches(self, only_names: Optional[Set[str]]) -> List[Dict]:
        selected: List[Dict] = []
        for entry in self.search_definitions:
            if not entry.get("enabled", True):
                continue
            name = self._effective_name(entry)
            if not name:
                continue
            if only_names and name.lower() not in only_names:
                continue
            selected.append(entry)
        return selected

    def run_once(self, only_names: Optional[Set[str]] = None):
        synced = self._sync_search_config_derived_fields()
        if synced:
            print("✓ search_config.json synchronized with derived name/raw_query fields")
            self.search_definitions = self._load_search_config(self.search_config_path)

        selected = self._selected_searches(only_names)
        if not selected:
            print("No enabled search definitions selected.")
            return

        for idx, search_def in enumerate(selected, start=1):
            print(f"\n[{idx}/{len(selected)}] {search_def.get('name', 'search_timeline')}")
            print("-" * 70)
            self._run_single_search(search_def)

        stats = self.api_manager.get_stats()
        print(f"\n{SEP}")
        print("RUN COMPLETE")
        print(SEP)
        print(f"Requests made: {stats['requests_made']}")
        print(f"Requests/min: {stats['requests_per_minute']}")

    def _run_single_search(self, search_def: Dict):
        name = self._effective_name(search_def)
        product = SearchQueryBuilder._normalize_product(search_def.get("product", "Top"))
        raw_query = self._effective_raw_query(search_def, now_dt=datetime.now(self.tz))
        if not raw_query:
            log.info("✗ Empty raw query; skipping")
            return

        search_url = SearchQueryBuilder.build_search_url(raw_query, product)
        paths = self._search_paths(name, product)

        max_pages = max(1, int(search_def.get("max_pages", DEFAULT_MAX_PAGES)))
        max_tweets_target = max(1, int(search_def.get("max_tweets_target", DEFAULT_MAX_TWEETS_TARGET)))

        # Keep logs inside this bucket so the project runs fully encapsulated.
        log = setup_logging(name, self.base_dir / "logs")

        log.info("\n[PHASE 1] SEARCH BUILD")
        log.info(f"  name: {name}")
        log.info(f"  rawQuery: {raw_query}")
        log.info(f"  URL: {search_url}")
        log.info(f"  product: {product}")
        log.info(
            "  thresholds: "
            f"min_faves={search_def.get('min_faves')} "
            f"min_retweets={search_def.get('min_retweets')} "
            f"min_replies={search_def.get('min_replies')}"
        )
        log.info(f"  tweet_target: {max_tweets_target}")
        log.info(f"  max_pages: {max_pages}")

        query_id = str(self.api_manager.get_query_id("SearchTimeline") or "").strip()
        if not query_id:
            log.info("✗ Missing search_timeline_query_id in config.json")
            return

        tx_id = str(self.api_manager.session.headers.get("x-client-transaction-id", "")).strip()
        csrf = str(self.api_manager.session.headers.get("x-csrf-token", "")).strip()

        log.info("\n[PHASE 2] SESSION INIT")
        log.info(f"  auth_loaded: {'yes' if bool(self.api_manager.session.headers.get('authorization')) else 'no'}")
        log.info(f"  csrf_loaded: {'yes' if bool(csrf) else 'no'}")
        log.info(f"  cookies_loaded: {len(self._cookie_snapshot())}")
        log.info(f"  operation_id: {query_id}")
        log.info(f"  tx_id: {self._mask_tx_id(tx_id)}")

        features_frozen = dict(FROZEN_SEARCH_FEATURES)
        variables_template = self._build_base_variables(search_def, raw_query, product)
        frozen_headers = self._build_frozen_headers(search_url)
        features_json_frozen = self._compact_json(features_frozen)

        warmup_status = None
        warmup_mode = str(search_def.get("continuity_warmup_mode", DEFAULT_CONTINUITY_WARMUP_MODE)).strip().lower()
        if warmup_mode not in {"off", "search_page_once"}:
            warmup_mode = DEFAULT_CONTINUITY_WARMUP_MODE
        if not self.dry_run and warmup_mode == "search_page_once" and ENABLE_SINGLE_WARMUP:
            try:
                warmup_resp = self.api_manager.session.get(search_url, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS)
                warmup_status = int(warmup_resp.status_code)
                dump_request(log, 0, method="GET", url=search_url, params={},
                             sent_headers=dict(self.api_manager.session.headers),
                             response=warmup_resp)
            except Exception:
                warmup_status = None
        log.info(f"  warmup_mode: {warmup_mode}")
        log.info(f"  warmup_status: {warmup_status if warmup_status is not None else 'skipped/failed'}")
        if warmup_status == 401:
            log.warning("  ⚠ warmup 401 — auth/csrf/cookies likely stale; "
                        "GraphQL 404s downstream are expected until re-auth")

        if self.dry_run:
            log.info("\n[DRY-RUN] Frozen request chain ready. No network requests sent.")
            return

        log.info("\n[PHASE 3] FIRST REQUEST")

        url = f"https://x.com/i/api/graphql/{query_id}/SearchTimeline"
        all_tweets: List[Dict] = []
        seen_ids: Set[str] = set()
        seen_cursors: Set[str] = set()
        cursor_history: List[str] = []
        per_page_status: List[Dict] = []
        page_request_payloads: List[Dict] = []
        stop_reason = "unknown"
        first_page_parser_debug: Dict = {}

        cursor: Optional[str] = None
        page = 1

        while page <= max_pages and len(all_tweets) < max_tweets_target:
            variables = dict(variables_template)
            if cursor:
                variables["cursor"] = cursor

            # CRITICAL FIX: Remove x-client-transaction-id on page 2+
            # X.com rejects requests with stale/reused tx-id (404 error)
            # Page 1 can use the session's tx-id, but subsequent pages should omit it
            request_headers = dict(frozen_headers)
            if page > 1 and "x-client-transaction-id" in request_headers:
                del request_headers["x-client-transaction-id"]

            params = {
                "variables": self._compact_json(variables),
                "features": features_json_frozen,
            }

            started = time.time()
            status = None
            response_json = None
            request_exception = None

            try:
                response = self.api_manager.session.get(
                    url,
                    params=params,
                    headers=request_headers,
                    timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
                )
                status = int(response.status_code)
                dump_request(log, page, method="GET", url=url, params=params,
                             sent_headers=request_headers, response=response)
                self.api_manager.update_rate_limit("SearchTimeline", response.headers)
                try:
                    response_json = response.json()
                except Exception:
                    response_json = None
            except Exception as exc:
                request_exception = str(exc)
                log.error(f"  page={page} request exception: {exc}")

            duration_ms = int((time.time() - started) * 1000)
            page_tweets_count = 0
            next_cursor = None
            duplicate_count = 0
            cursor_source = None
            entry_type_counts = {}
            skipped_entries = []

            if status == 200 and isinstance(response_json, dict):
                parsed = self._parse_page(self._extract_instructions(response_json), seen_ids)
                all_tweets.extend(parsed["tweets"])
                page_tweets_count = len(parsed["tweets"])
                next_cursor = parsed.get("next_cursor")
                duplicate_count = int(parsed.get("duplicate_count", 0))
                cursor_source = parsed.get("selected_cursor_source")
                entry_type_counts = parsed.get("entry_type_counts", {})
                skipped_entries = parsed.get("skipped_entries", [])

                if page == 1:
                    first_page_parser_debug = {
                        "entry_type_counts": entry_type_counts,
                        "cursor_candidates": parsed.get("cursor_candidates", []),
                        "skipped_entries": skipped_entries,
                    }

            page_payload = {
                "captured_at": datetime.now(self.tz).isoformat(),
                "page": page,
                "cursor": cursor,
                "status": status,
                "request_exception": request_exception,
                "url": url,
                "search_url": search_url,
                "headers": request_headers,
                "cookies": self._cookie_snapshot(),
                "variables": variables,
                "features": features_frozen,
                "params_encoded": urlencode(params),
                "duration_ms": duration_ms,
                "tweet_count": page_tweets_count,
                "duplicate_count": duplicate_count,
                "next_cursor": next_cursor,
                "cursor_source": cursor_source,
                "entry_type_counts": entry_type_counts,
                "skipped_entries_count": len(skipped_entries),
                "response_top_level_keys": list(response_json.keys()) if isinstance(response_json, dict) else [],
            }
            if page == 1:
                page_payload["warmup_status"] = warmup_status
                page_payload["warmup_mode"] = warmup_mode
            page_request_payloads.append(page_payload)
            self._save_page_json(paths["json_dir"], paths["safe_name"], page_payload)

            per_page_status.append(
                {
                    "page": page,
                    "status": status,
                    "duration_ms": duration_ms,
                    "tweets_added": page_tweets_count,
                    "duplicates": duplicate_count,
                    "cursor_in": cursor,
                    "cursor_out": next_cursor,
                }
            )

            if page == 1:
                log.info(
                    f"  status={status} tweets_extracted={page_tweets_count} "
                    f"cursor_found={'yes' if bool(next_cursor) else 'no'}"
                )

            log.info("\n[PHASE 4] PAGINATION")
            log.info(
                f"  page={page} status={status} added={page_tweets_count} total={len(all_tweets)} "
                f"cursor={str(cursor)[:36] if cursor else 'none'} "
                f"next={str(next_cursor)[:36] if next_cursor else 'none'} "
                f"duration={duration_ms}ms dup={duplicate_count}"
            )

            if status != 200:
                stop_reason = "continuity_break_404" if status == 404 else "request_failed_non200"
                break
            # BUG FIX: Don't stop on zero new tweets - page might be all duplicates
            # but cursor is still valid. Keep going until we hit max_tweets or cursor exhaustion.
            # if page_tweets_count <= 0:
            #     stop_reason = "empty_page"
            #     break
            if len(all_tweets) >= max_tweets_target:
                stop_reason = "max_tweets_reached"
                break
            if not next_cursor:
                stop_reason = "cursor_exhausted"
                break
            if next_cursor in seen_cursors:
                stop_reason = "repeated_cursor"
                break

            # Cursor continuity update: ONLY cursor changes between pages
            seen_cursors.add(next_cursor)
            cursor_history.append(next_cursor)
            cursor = next_cursor
            page += 1

        if page > max_pages and stop_reason == "unknown":
            stop_reason = "page_limit_reached"

        if first_page_parser_debug:
            self._save_first_page_debug(paths["debug_dir"], paths["safe_name"], first_page_parser_debug)

        continuity_diag = self._write_continuity_diagnostic(
            json_dir=paths["json_dir"],
            safe_name=paths["safe_name"],
            search_name=name,
            stop_reason=stop_reason,
            query_id=query_id,
            tx_id=tx_id,
            page_payloads=page_request_payloads,
        )

        log.info("\n[PHASE 5] STOP CONDITION")
        log.info(f"  reason: {stop_reason}")
        log.info(f"  pages_attempted: {len(per_page_status)}")
        log.info(f"  tweets_collected: {len(all_tweets)}")
        log.info("\n[CONTINUITY DIAGNOSTIC]")
        log.info(f"  page1_status: {continuity_diag['signals'].get('page1_status')}")
        log.info(f"  page2_status: {continuity_diag['signals'].get('page2_status')}")
        log.info(f"  cursor_chain_ok: {continuity_diag['signals'].get('cursor_chain_ok')}")
        log.info(
            "  diffs: "
            f"headers={continuity_diag['signals'].get('header_diff_count')} "
            f"cookies={continuity_diag['signals'].get('cookie_diff_count')} "
            f"variables={continuity_diag['signals'].get('variable_diff_count')} "
            f"non_cursor_variables={continuity_diag['signals'].get('non_cursor_variable_diff_count')} "
            f"features={continuity_diag['signals'].get('feature_diff_count')}"
        )
        if continuity_diag["probable_break_causes"]:
            log.info(f"  probable_causes: {', '.join(continuity_diag['probable_break_causes'])}")
        if EMIT_VERBOSE_CONTINUITY_DIFF:
            fp = continuity_diag.get("fingerprints", {})
            if fp:
                log.info("  fingerprints:")
                log.info(f"    headers: p1={fp.get('headers_page1','')[:12]} p2={fp.get('headers_page2','')[:12]}")
                log.info(f"    cookies: p1={fp.get('cookies_page1','')[:12]} p2={fp.get('cookies_page2','')[:12]}")
                log.info(f"    vars_no_cursor: p1={fp.get('variables_no_cursor_page1','')[:12]} p2={fp.get('variables_no_cursor_page2','')[:12]}")
        log.info(f"  diagnostic_file: {continuity_diag['path']}")

        result = self._persist_outputs(
            search_name=name,
            product=product,
            raw_query=raw_query,
            search_url=search_url,
            query_id=query_id,
            tx_id=tx_id,
            paths=paths,
            all_tweets=all_tweets,
            pages_completed=len(per_page_status),
            cursor_history=cursor_history,
            stop_reason=stop_reason,
            per_page_status=per_page_status,
        )

        newest = result["newest_dt"].isoformat() if result["newest_dt"] else "unknown"
        oldest = result["oldest_dt"].isoformat() if result["oldest_dt"] else "unknown"

        log.info(f"  saved_unique: {result['saved_unique']}")
        log.info(f"  duplicate_overwrites: {result['duplicate_overwrites']}")
        log.info(f"  timestamps newest={newest} oldest={oldest}")
        log.info(f"  TXT: {result['output_txt']}")
        log.info(f"  JSON SUMMARY: {result['output_json']}")
        log.info(f"  JSON FILES DIR: {paths['json_dir']}")
        log.info(f"  DEBUGGING DIR: {paths['debug_dir']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SearchTimeline exact replay monitor")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Main config path")
    parser.add_argument("--search-config", default=DEFAULT_SEARCH_CONFIG_PATH, help="Search config path")
    parser.add_argument("--name", action="append", help="Run only this search name (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="Build chain only; no network")
    return parser.parse_args()


def main():
    args = parse_args()

    print(SEP)
    print("Twitter SearchTimeline EXACT Replay Monitor")
    print(SEP)
    print("Deterministic chain: frozen op-id + frozen features + cursor-only continuation")

    monitor = SearchTimelineExactReplayMonitor(
        config_path=args.config,
        search_config_path=args.search_config,
        dry_run=args.dry_run,
    )

    selected_names = {name.lower() for name in (args.name or []) if str(name).strip()}
    monitor.run_once(only_names=selected_names or None)


if __name__ == "__main__":
    main()
