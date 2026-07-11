#!/usr/bin/env python3
"""
Twitter SearchTimeline Monitor

Uses existing project architecture:
- api_manager.py for session/auth/retries/rate-limits
- storage_manager.py for state/logging/formatting conventions
- fetch_historical_tweets_hybrid.py parser helpers for tweet extraction

Behavior:
- Builds X search queries dynamically from search_config.json
- Monitors SearchTimeline GraphQL continuously
- Uses deterministic bounded pagination
- Deduplicates by tweet ID and updates metrics on reappearance
- Stores raw payloads + parsed exports under data/SEARCH_TIMELINE
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import quote


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

DEFAULT_SEARCH_PRIORITY_POLICIES: Dict[int, Dict] = {
    1: {"poll_interval_seconds": 90, "pagination_depth": 8, "max_retries": 4},
    2: {"poll_interval_seconds": 150, "pagination_depth": 7, "max_retries": 4},
    3: {"poll_interval_seconds": 240, "pagination_depth": 6, "max_retries": 3},
    4: {"poll_interval_seconds": 360, "pagination_depth": 5, "max_retries": 3},
    5: {"poll_interval_seconds": 540, "pagination_depth": 4, "max_retries": 3},
    6: {"poll_interval_seconds": 780, "pagination_depth": 3, "max_retries": 2},
    7: {"poll_interval_seconds": 1080, "pagination_depth": 2, "max_retries": 2},
}


class SearchQueryBuilder:
    """Build rawQuery and human search URL from config."""

    @staticmethod
    def _sanitize_term(value: str) -> str:
        text = str(value or "").strip()
        return re.sub(r"\s+", " ", text)

    @staticmethod
    def _ensure_handle(value: str) -> str:
        handle = str(value or "").strip().lstrip("@")
        handle = re.sub(r"[^A-Za-z0-9_]", "", handle)
        return handle

    @staticmethod
    def _quote_phrase(value: str) -> str:
        text = SearchQueryBuilder._sanitize_term(value).replace('"', "")
        if not text:
            return ""
        return f"\"{text}\""

    @staticmethod
    def _normalize_product(value: str) -> str:
        candidate = str(value or "Top").strip().title()
        return candidate if candidate in VALID_PRODUCTS else "Top"

    @staticmethod
    def build_raw_query(search_def: Dict, now_dt: datetime) -> str:
        if search_def.get("raw_query"):
            return str(search_def["raw_query"]).strip()
        if bool(search_def.get("preserve_exact_query", False)):
            explicit = str(search_def.get("exact_query") or "").strip()
            if explicit:
                return explicit

        parts: List[str] = []

        include_keywords = [
            SearchQueryBuilder._sanitize_term(term)
            for term in search_def.get("include_keywords", [])
            if SearchQueryBuilder._sanitize_term(term)
        ]
        if include_keywords:
            if len(include_keywords) == 1:
                parts.append(include_keywords[0])
            else:
                parts.append("(" + " OR ".join(include_keywords) + ")")

        for phrase in search_def.get("exact_phrases", []):
            clean_phrase = SearchQueryBuilder._sanitize_term(phrase).replace('"', "")
            if clean_phrase:
                parts.append(clean_phrase)

        for keyword in search_def.get("exclude_keywords", []):
            clean = SearchQueryBuilder._sanitize_term(keyword)
            if not clean:
                continue
            if " " in clean:
                parts.append(f"-{SearchQueryBuilder._quote_phrase(clean)}")
            else:
                parts.append(f"-{clean}")

        for from_account in search_def.get("from_accounts", []):
            handle = SearchQueryBuilder._ensure_handle(from_account)
            if handle:
                parts.append(f"from:{handle}")

        for to_account in search_def.get("to_accounts", []):
            handle = SearchQueryBuilder._ensure_handle(to_account)
            if handle:
                parts.append(f"to:{handle}")

        for mention in search_def.get("mentions", []):
            handle = SearchQueryBuilder._ensure_handle(mention)
            if handle:
                parts.append(f"@{handle}")

        lang = SearchQueryBuilder._sanitize_term(str(search_def.get("lang", "")))
        if lang:
            parts.append(f"lang:{lang}")

        min_replies = search_def.get("min_replies")
        if min_replies is not None and str(min_replies).strip():
            parts.append(f"min_replies:{int(min_replies)}")

        min_faves = search_def.get("min_faves")
        if min_faves is not None and str(min_faves).strip():
            parts.append(f"min_faves:{int(min_faves)}")

        min_retweets = search_def.get("min_retweets")
        if min_retweets is not None and str(min_retweets).strip():
            parts.append(f"min_retweets:{int(min_retweets)}")

        since = SearchQueryBuilder._sanitize_term(str(search_def.get("since", "")))
        until = SearchQueryBuilder._sanitize_term(str(search_def.get("until", "")))
        since_days = search_def.get("since_days")

        if not since and since_days is not None:
            since_date = (now_dt - timedelta(days=int(since_days))).date()
            since = since_date.isoformat()
        if (not until and since_days is not None) and not bool(search_def.get("preserve_exact_query", False)):
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
        normalized_product = SearchQueryBuilder._normalize_product(product)
        encoded_query = quote(raw_query, safe="()")
        url = f"https://x.com/search?q={encoded_query}"
        product_filter_map = {
            "Top": "top",
            "Latest": "live",
            "Media": "media",
            "People": "user",
        }
        filter_value = product_filter_map.get(normalized_product, "")
        if filter_value:
            url += f"&f={filter_value}"
        url += "&src=typed_query"
        return url

    @staticmethod
    def output_basename(search_def: Dict, date_str: str) -> str:
        raw_name = str(search_def.get("output_slug") or "").strip()
        if not raw_name:
            keywords = [
                SearchQueryBuilder._sanitize_term(k).lower()
                for k in search_def.get("include_keywords", [])
                if SearchQueryBuilder._sanitize_term(k)
            ]
            raw_name = "_".join(keywords[:8]) if keywords else str(search_def.get("name", "search_timeline"))
        raw_name = re.sub(r"[^a-zA-Z0-9_]+", "_", raw_name.lower())
        raw_name = re.sub(r"_+", "_", raw_name).strip("_") or "search_timeline"

        suffixes = []
        if search_def.get("min_faves") is not None:
            suffixes.append(f"min{int(search_def['min_faves'])}likes")
        if search_def.get("min_retweets") is not None:
            suffixes.append(f"min{int(search_def['min_retweets'])}retweets")
        if search_def.get("min_replies") is not None:
            suffixes.append(f"min{int(search_def['min_replies'])}replies")

        metric_suffix = f"_{'_'.join(suffixes)}" if suffixes else ""
        return f"{raw_name}{metric_suffix}_{date_str}"


class SearchTimelineMonitor:
    """Continuous SearchTimeline monitor using existing infrastructure."""

    def __init__(
        self,
        config_path: str = "config.json",
        search_config_path: str = "search_config.json",
        dry_run: bool = False,
    ):
        self.base_dir = Path(__file__).parent
        self.config_path = str(self._resolve_path(config_path))
        self.search_config_path = str(self._resolve_path(search_config_path))
        self.dry_run = dry_run

        print("Initializing SearchTimeline monitor...")
        self.api_manager = APIManager(self.config_path, state_dir=self.base_dir / "data" / "STATE")
        self.storage_manager = StorageManager(self.base_dir, timezone="Asia/Tehran")
        self.fetcher = TwitterHistoricalFetcher(self.config_path)
        self.fetcher.api_manager = self.api_manager
        self.fetcher.storage_manager = self.storage_manager

        self.config = self.api_manager.config
        self.tz = pytz.timezone("Asia/Tehran")
        self.search_definitions = self._load_search_config(self.search_config_path)
        self.priority_policies = self._load_priority_policies()

        self.last_fetch: Dict[str, datetime] = {}
        self.error_count: Dict[str, int] = defaultdict(int)

        print("✓ SearchTimeline monitor initialized")
        print(f"  - Loaded {len(self.search_definitions)} search config entries")

    def _resolve_path(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.base_dir / candidate
        return candidate

    def _load_search_config(self, path: str) -> List[Dict]:
        search_cfg_path = Path(path)
        if not search_cfg_path.exists():
            raise FileNotFoundError(f"Search config not found: {search_cfg_path}")
        with open(search_cfg_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, list):
            raise ValueError("search_config.json must be a JSON list")
        return [entry for entry in payload if isinstance(entry, dict)]

    def _load_priority_policies(self) -> Dict[int, Dict]:
        policies: Dict[int, Dict] = {}
        overrides = self.config.get("search_priority_policies", {}) or {}
        for priority, defaults in DEFAULT_SEARCH_PRIORITY_POLICIES.items():
            over = overrides.get(str(priority), {}) if isinstance(overrides, dict) else {}
            policies[priority] = {
                "priority": priority,
                "poll_interval_seconds": int(over.get("poll_interval_seconds", defaults["poll_interval_seconds"])),
                "pagination_depth": int(over.get("pagination_depth", defaults["pagination_depth"])),
                "max_retries": int(over.get("max_retries", defaults["max_retries"])),
            }
        return policies

    def _policy_for_search(self, search_def: Dict) -> Dict:
        priority = int(search_def.get("polling_priority", 4))
        if priority not in self.priority_policies:
            priority = 7
        policy = dict(self.priority_policies[priority])
        return policy

    def _compact_json(self, payload: Dict) -> str:
        return json.dumps(payload, separators=(",", ":"))

    def _random_human_pause(self, bucket: str = "between_pages"):
        sim = self.config.get("anti_bot_simulation", {})
        if not sim.get("enabled", True):
            return
        delays = sim.get("delays_seconds", {})
        if bucket == "between_searches":
            min_d = float(delays.get("between_accounts_min", 2))
            max_d = float(delays.get("between_accounts_max", 6))
        elif bucket == "search_retry":
            min_d = float(delays.get("replies_retry_min", 1))
            max_d = float(delays.get("replies_retry_max", 3))
        elif bucket == "between_cycles":
            min_d = float(delays.get("between_cycles_min", 0))
            max_d = float(delays.get("between_cycles_max", 60))
        else:
            min_d = float(delays.get("between_pages_min", 2))
            max_d = float(delays.get("between_pages_max", 6))
        if max_d < min_d:
            max_d = min_d
        if max_d <= 0:
            return
        time.sleep(random.uniform(min_d, max_d))

    def _normalize_product(self, search_def: Dict) -> str:
        return SearchQueryBuilder._normalize_product(str(search_def.get("product", "Top")))

    def _search_features(self, search_def: Dict) -> Dict:
        features = dict(self.fetcher._timeline_features())
        global_overrides = self.config.get("search_timeline_feature_overrides", {})
        local_overrides = search_def.get("feature_overrides", {})

        if isinstance(global_overrides, dict):
            for key, value in global_overrides.items():
                features[str(key)] = value
        if isinstance(local_overrides, dict):
            for key, value in local_overrides.items():
                features[str(key)] = value
        return features

    def _search_variables(self, search_def: Dict, raw_query: str, product: str, cursor: Optional[str] = None) -> Dict:
        count = int(search_def.get("count", 20))
        count = max(1, min(count, 100))

        variables = {
            "rawQuery": raw_query,
            "count": count,
            "querySource": str(search_def.get("query_source", "typed_query")),
            "product": product,
            "withGrokTranslatedBio": bool(search_def.get("with_grok_translated_bio", True)),
            "withQuickPromoteEligibilityTweetFields": bool(
                search_def.get("with_quick_promote_eligibility_tweet_fields", False)
            ),
        }
        if cursor:
            variables["cursor"] = cursor

        variable_overrides = search_def.get("variable_overrides", {})
        if isinstance(variable_overrides, dict):
            for key, value in variable_overrides.items():
                variables[str(key)] = value
        return variables

    def _search_field_toggles(self, search_def: Dict) -> Dict:
        toggles: Dict = {}
        overrides = search_def.get("field_toggle_overrides", {})
        if isinstance(overrides, dict):
            for key, value in overrides.items():
                toggles[str(key)] = value
        return toggles

    def _search_paths(self, search_def: Dict, product: str) -> Dict[str, Path]:
        search_name = str(search_def.get("name", "search_timeline")).strip() or "search_timeline"
        safe_name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", search_name).strip("_") or "search_timeline"

        root_dir = self.storage_manager.search_timeline_dir / safe_name / product
        debug_dir = root_dir / "DEBUGGING"
        root_dir.mkdir(parents=True, exist_ok=True)
        debug_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_output_structure(root_dir=root_dir, safe_name=safe_name, debug_dir=debug_dir)

        return {
            "root": root_dir,
            "debug": debug_dir,
            "safe_name": safe_name,
        }

    def _move_file_with_unique_name(self, source_file: Path, destination_file: Path):
        destination = destination_file
        if destination.exists():
            stem = destination.stem
            suffix = destination.suffix
            counter = 1
            while True:
                candidate = destination.with_name(f"{stem}_{counter}{suffix}")
                if not candidate.exists():
                    destination = candidate
                    break
                counter += 1
        shutil.move(str(source_file), str(destination))

    def _migrate_legacy_output_structure(self, root_dir: Path, safe_name: str, debug_dir: Path):
        legacy_raw = root_dir / "RAW"
        legacy_parsed = root_dir / "PARSED"
        has_legacy = legacy_raw.exists() or legacy_parsed.exists()
        if has_legacy:
            print("  Detected legacy RAW/PARSED structure; flattening into search root...")

        if legacy_raw.exists():
            for raw_file in sorted(legacy_raw.glob("*")):
                if raw_file.is_file():
                    target = root_dir / f"{safe_name.upper()}__{raw_file.name}"
                    self._move_file_with_unique_name(raw_file, target)
            try:
                legacy_raw.rmdir()
            except Exception:
                pass

        if legacy_parsed.exists():
            for parsed_file in sorted(legacy_parsed.glob("*")):
                if not parsed_file.is_file():
                    continue
                if parsed_file.name == "latest_run.txt":
                    target_name = f"{safe_name.upper()}.txt"
                elif parsed_file.name == "latest_run.json":
                    target_name = f"{safe_name.upper()}.json"
                elif "debug_first_page" in parsed_file.name.lower():
                    target_name = f"{safe_name.upper()}__{parsed_file.name}"
                    target = debug_dir / target_name
                    self._move_file_with_unique_name(parsed_file, target)
                    continue
                else:
                    target_name = f"{safe_name.upper()}__{parsed_file.name}"
                target = root_dir / target_name
                self._move_file_with_unique_name(parsed_file, target)
            try:
                legacy_parsed.rmdir()
            except Exception:
                pass

        for root_file in sorted(root_dir.glob("*")):
            if not root_file.is_file():
                continue
            lname = root_file.name.lower()
            if "__debug_first_page" in lname or lname.startswith("debug_first_page"):
                target = debug_dir / root_file.name
                self._move_file_with_unique_name(root_file, target)

    def _rate_limit_state(self) -> Dict:
        return self.api_manager.rate_limits.get("SearchTimeline", {})

    def _should_fetch_search(self, key: str, interval_seconds: int) -> bool:
        last_fetch = self.last_fetch.get(key)
        if not last_fetch:
            return True
        elapsed = (datetime.now(self.tz) - last_fetch).total_seconds()
        return elapsed >= interval_seconds

    def _tweet_datetime(self, tweet: Dict) -> Optional[datetime]:
        raw = tweet.get("raw_timestamp")
        if isinstance(raw, str) and raw.strip():
            try:
                return datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y").astimezone(self.tz)
            except Exception:
                pass
        return None

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

        card_legacy = tweet_obj.get("card", {}).get("legacy", {})
        bindings = card_legacy.get("binding_values", [])
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
            quoted_legacy = quoted_obj.get("legacy", {})
            quoted_media = self._extract_media_from_legacy(quoted_legacy if isinstance(quoted_legacy, dict) else {})
            quoted_image_urls.update(quoted_media["image_urls"])
            quoted_video_urls.update(quoted_media["video_urls"])
            quoted_m3u8_urls.update(quoted_media["m3u8_urls"])

            quoted_card = self._extract_card_links(quoted_obj)
            for url in quoted_card["card_urls"]:
                card_links["card_urls"].append(url)
            for url in quoted_card["card_image_urls"]:
                card_links["card_image_urls"].append(url)
            for url in quoted_card["external_urls"]:
                card_links["external_urls"].append(url)

        entity_external_urls: Set[str] = set()
        entities = parsed.get("entities", {}) if isinstance(parsed.get("entities"), dict) else {}
        for url_entry in entities.get("urls", []):
            if not isinstance(url_entry, dict):
                continue
            expanded = str(url_entry.get("expanded") or url_entry.get("short") or "").strip()
            if expanded.startswith("http"):
                entity_external_urls.add(expanded)

        card_urls = sorted(set(card_links["card_urls"]))
        card_image_urls = sorted(set(card_links["card_image_urls"]))
        external_urls = sorted(set(card_links["external_urls"]) | entity_external_urls)

        parsed["media"] = {
            "tweet_urls": [parsed.get("url")] if parsed.get("url") else [],
            "image_urls": own_media["image_urls"],
            "video_urls": own_media["video_urls"],
            "m3u8_urls": own_media["m3u8_urls"],
            "quoted_image_urls": sorted(quoted_image_urls),
            "quoted_video_urls": sorted(quoted_video_urls),
            "quoted_m3u8_urls": sorted(quoted_m3u8_urls),
            "card_urls": card_urls,
            "card_image_urls": card_image_urls,
            "external_urls": external_urls,
        }
        return parsed

    def _extract_bottom_cursor_from_entry(self, entry: Dict) -> Optional[str]:
        content = entry.get("content", {})
        entry_id = str(entry.get("entryId", ""))
        if not isinstance(content, dict):
            return None

        if content.get("__typename") == "TimelineTimelineCursor":
            if str(content.get("cursorType", "")).lower() == "bottom":
                value = content.get("value")
                return str(value) if value else None

        if entry_id.startswith("cursor-bottom-"):
            value = content.get("value")
            return str(value) if value else None

        return None

    def _collect_cursor_candidates_from_entry(self, entry: Dict, source_path: str) -> List[Dict]:
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

        # Scan module items for nested cursors
        if isinstance(content.get("items"), list):
            for idx, item_entry in enumerate(content.get("items", [])):
                if not isinstance(item_entry, dict):
                    continue
                nested_item = item_entry.get("item", {})
                if not isinstance(nested_item, dict):
                    continue
                nested_content = nested_item.get("content", {})
                if isinstance(nested_content, dict):
                    nested_value = nested_content.get("value")
                    if nested_value:
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

    def _extract_search_instructions(self, response_json: Dict) -> List[Dict]:
        return (
            response_json.get("data", {})
            .get("search_by_raw_query", {})
            .get("search_timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )

    def _parse_search_page(
        self,
        instructions: List[Dict],
        seen_ids: Set[str],
        capture_debug: bool = False,
    ) -> Dict:
        page_tweets: List[Dict] = []
        cursor_candidates: List[Dict] = []
        next_cursor: Optional[str] = None
        has_entries = False
        timeline_item_count = 0
        timeline_module_count = 0
        oldest_dt: Optional[datetime] = None
        newest_dt: Optional[datetime] = None
        entry_type_counts: Dict[str, int] = defaultdict(int)
        skipped_entries: List[Dict] = []
        processed_entries: List[Dict] = []
        selected_cursor_source: Optional[str] = None

        def update_bounds(tweet_dt: Optional[datetime]):
            nonlocal oldest_dt, newest_dt
            if not tweet_dt:
                return
            if oldest_dt is None or tweet_dt < oldest_dt:
                oldest_dt = tweet_dt
            if newest_dt is None or tweet_dt > newest_dt:
                newest_dt = tweet_dt

        def log_skip(entry_id: str, typename: str, reason: str):
            payload = {"entry_id": entry_id or "unknown", "typename": typename or "unknown", "reason": reason}
            skipped_entries.append(payload)
            self.storage_manager.log_event(
                "parser_skips",
                f"SearchTimeline parser skip | entry_id={payload['entry_id']} typename={payload['typename']} reason={payload['reason']}",
            )

        def try_add_tweet(tweet_result_wrapper: Dict, entry_id: str, typename: str, source_path: str) -> bool:
            tweet_obj = self.fetcher._unwrap_tweet_result(tweet_result_wrapper)
            if not tweet_obj:
                log_skip(entry_id, typename, f"unwrap_failed:{source_path}")
                return True
            parsed = self.fetcher._parse_tweet(tweet_obj)
            if not parsed:
                log_skip(entry_id, typename, f"parse_failed:{source_path}")
                return True
            parsed = self._enrich_search_tweet(parsed, tweet_obj)
            tweet_dt = self._tweet_datetime(parsed)
            update_bounds(tweet_dt)
            tweet_id = parsed.get("id")
            if tweet_id and tweet_id not in seen_ids:
                seen_ids.add(tweet_id)
                page_tweets.append(parsed)
                processed_entries.append(
                    {
                        "entry_id": entry_id,
                        "typename": typename,
                        "source_path": source_path,
                        "tweet_id": str(tweet_id),
                        "status": "added",
                    }
                )
            elif tweet_id:
                processed_entries.append(
                    {
                        "entry_id": entry_id,
                        "typename": typename,
                        "source_path": source_path,
                        "tweet_id": str(tweet_id),
                        "status": "duplicate_ignored",
                    }
                )
            else:
                log_skip(entry_id, typename, f"missing_tweet_id:{source_path}")
            return True

        for inst in instructions:
            inst_type = str(inst.get("type", ""))
            entry_type_counts[f"instruction:{inst_type or 'unknown'}"] += 1

            if inst_type == "TimelineReplaceEntry":
                entry = inst.get("entry", {})
                if isinstance(entry, dict):
                    cursor_candidates.extend(self._collect_cursor_candidates_from_entry(entry, "TimelineReplaceEntry.entry"))
                continue

            if inst_type == "TimelinePinEntry":
                entry = inst.get("entry", {})
                if isinstance(entry, dict):
                    cursor_candidates.extend(self._collect_cursor_candidates_from_entry(entry, "TimelinePinEntry.entry"))
                    entry_id = str(entry.get("entryId", ""))
                    content = entry.get("content", {})
                    typename = str(content.get("__typename", "")) if isinstance(content, dict) else ""
                    if isinstance(content, dict):
                        item_content = content.get("itemContent", {})
                        if isinstance(item_content, dict):
                            tweet_results = item_content.get("tweet_results", {})
                            try_add_tweet(tweet_results, entry_id, typename, "TimelinePinEntry.entry.content.itemContent.tweet_results")
                continue

            if inst_type != "TimelineAddEntries":
                self.storage_manager.log_event(
                    "parser_skips",
                    f"SearchTimeline parser skip | entry_id=instruction typename={inst_type or 'unknown'} reason=unsupported_instruction_type",
                )
                continue

            entries = inst.get("entries", [])
            if entries:
                has_entries = True

            for entry in entries:
                if not isinstance(entry, dict):
                    continue

                entry_id = str(entry.get("entryId", ""))
                content = entry.get("content", {})
                typename = str(content.get("__typename", "")) if isinstance(content, dict) else "unknown"
                entry_type_counts[f"entry:{typename or 'unknown'}"] += 1
                cursor_candidates.extend(self._collect_cursor_candidates_from_entry(entry, "TimelineAddEntries.entries"))
                item_content = content.get("itemContent", {}) if isinstance(content, dict) else {}

                if entry_id.startswith("tweet-") or (
                    isinstance(content, dict) and str(content.get("__typename", "")).strip() == "TimelineTimelineItem"
                ):
                    timeline_item_count += 1
                    tweet_results = item_content.get("tweet_results", {})
                    try_add_tweet(tweet_results, entry_id, typename, "TimelineAddEntries.entries[].content.itemContent.tweet_results")
                    continue

                if isinstance(content, dict) and isinstance(content.get("items"), list):
                    timeline_module_count += 1
                    for idx, item_entry in enumerate(content.get("items", [])):
                        item = item_entry.get("item", {}) if isinstance(item_entry, dict) else {}
                        module_item_content = item.get("itemContent", {}) if isinstance(item, dict) else {}
                        module_typename = str(item.get("content", {}).get("__typename", "")) if isinstance(item.get("content"), dict) else typename
                        module_entry_id = f"{entry_id}#item{idx}"
                        tweet_results = module_item_content.get("tweet_results", {})
                        if not tweet_results:
                            log_skip(module_entry_id, module_typename, "module_item_without_tweet_results")
                            continue
                        try_add_tweet(
                            tweet_results,
                            module_entry_id,
                            module_typename,
                            "TimelineAddEntries.entries[].content.items[].item.itemContent.tweet_results",
                        )
                    continue

                if isinstance(content, dict):
                    if entry_id.startswith("promoted-") or "promoted" in entry_id.lower():
                        log_skip(entry_id, typename, "promoted_entry")
                    else:
                        log_skip(entry_id, typename, "unsupported_entry_shape")

        if cursor_candidates:
            cursor_candidates = sorted(cursor_candidates, key=lambda c: int(c.get("score", 0)), reverse=True)
            best = cursor_candidates[0]
            next_cursor = str(best.get("value", "")).strip() or None
            selected_cursor_source = str(best.get("source_path", "")) if next_cursor else None

        return {
            "tweets": page_tweets,
            "next_cursor": next_cursor,
            "has_entries": has_entries,
            "timeline_item_count": timeline_item_count,
            "timeline_module_count": timeline_module_count,
            "reached_time_limit": False,
            "oldest_dt": oldest_dt,
            "newest_dt": newest_dt,
            "entry_type_counts": dict(entry_type_counts),
            "cursor_candidates": cursor_candidates,
            "selected_cursor_source": selected_cursor_source,
            "skipped_entries": skipped_entries,
            "processed_entries": processed_entries if capture_debug else [],
        }

    def _save_raw_payload(self, root_dir: Path, safe_name: str, page: int, response_json: Dict) -> Path:
        timestamp = datetime.now(self.tz).strftime("%Y%m%d_%H%M%S_%f")
        output_file = root_dir / f"{safe_name.upper()}__raw_page_{page:03d}_{timestamp}.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(response_json, f, ensure_ascii=False, indent=2)
        return output_file

    def _tweet_sort_key(self, tweet: Dict) -> float:
        dt = self._tweet_datetime(tweet)
        if dt is None:
            return 0.0
        return dt.timestamp()

    def _persist_run_exports(
        self,
        search_def: Dict,
        product: str,
        raw_query: str,
        paths: Dict[str, Path],
        fetched_tweets: List[Dict],
        run_metadata: Dict,
        parser_debug: Dict,
    ) -> Dict:
        root_dir = paths["root"]
        safe_name = str(paths["safe_name"])
        now_dt = datetime.now(self.tz)

        dedup_map: Dict[str, Dict] = {}
        duplicate_updates = 0
        for tweet in fetched_tweets:
            tweet_id = str(tweet.get("id") or "").strip()
            if not tweet_id:
                continue
            if tweet_id in dedup_map:
                duplicate_updates += 1
            dedup_map[tweet_id] = tweet
            self.storage_manager.register_tweet(
                tweet_id=tweet_id,
                account=str(tweet.get("account", "unknown")),
                stored_in=[f"SEARCH_TIMELINE/{str(search_def.get('name', 'search_timeline'))}/{product}"],
            )

        tweets_sorted = sorted(dedup_map.values(), key=self._tweet_sort_key, reverse=True)
        current_count = len(tweets_sorted)
        oldest_dt = self._tweet_datetime(tweets_sorted[-1]) if tweets_sorted else None
        newest_dt = self._tweet_datetime(tweets_sorted[0]) if tweets_sorted else None

        query_id = self.api_manager.get_query_id("SearchTimeline") or ""
        tx_id = str(self.api_manager.session.headers.get("x-client-transaction-id", "")).strip()
        masked_tx = f"{tx_id[:18]}...{tx_id[-12:]}" if len(tx_id) > 36 else tx_id

        payload = {
            "generated_at": now_dt.isoformat(),
            "search_name": search_def.get("name", "search_timeline"),
            "product": product,
            "raw_query": raw_query,
            "query_id": query_id,
            "tx_id": masked_tx,
            "metadata": run_metadata,
            "counts": {
                "tweets_extracted_total": len(fetched_tweets),
                "new_ids_this_run": current_count,
                "duplicate_ids_overwritten": duplicate_updates,
                "skipped_entries_total": int(parser_debug.get("skipped_entries_total", 0)),
            },
            "tweets": tweets_sorted,
        }

        output_json = root_dir / f"{safe_name.upper()}.json"
        output_txt = root_dir / f"{safe_name.upper()}.txt"

        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        text_body = self.storage_manager._format_tweets(tweets_sorted) if tweets_sorted else "No tweets extracted in this run."
        with open(output_txt, "w", encoding="utf-8") as f:
            f.write(text_body)

        debug_dir = paths["debug"]
        debug_prefix = debug_dir / f"{safe_name.upper()}__debug_first_page"
        with open(debug_dir / f"{safe_name.upper()}__debug_first_page_entry_types.json", "w", encoding="utf-8") as f:
            json.dump(parser_debug.get("entry_type_counts", {}), f, ensure_ascii=False, indent=2)
        with open(debug_dir / f"{safe_name.upper()}__debug_first_page_cursor_candidates.json", "w", encoding="utf-8") as f:
            json.dump(parser_debug.get("cursor_candidates", []), f, ensure_ascii=False, indent=2)
        with open(debug_dir / f"{safe_name.upper()}__debug_first_page_skipped_entries.json", "w", encoding="utf-8") as f:
            json.dump(parser_debug.get("skipped_entries", []), f, ensure_ascii=False, indent=2)
        with open(debug_dir / f"{safe_name.upper()}__debug_first_page_extract_map.json", "w", encoding="utf-8") as f:
            json.dump(parser_debug.get("processed_entries", []), f, ensure_ascii=False, indent=2)

        return {
            "new_count": current_count,
            "duplicate_updates": duplicate_updates,
            "current_count": current_count,
            "oldest_dt": oldest_dt,
            "newest_dt": newest_dt,
            "output_json": output_json,
            "output_txt": output_txt,
            "debug_prefix": str(debug_prefix),
        }

    def _request_search_timeline_page(
        self,
        *,
        query_id: str,
        raw_query: str,
        search_url: str,
        search_def: Dict,
        product: str,
        cursor: Optional[str],
        max_retries: int,
    ) -> Optional[Dict]:
        endpoint = "SearchTimeline"
        url = f"https://x.com/i/api/graphql/{query_id}/SearchTimeline"

        variables = self._search_variables(search_def, raw_query, product, cursor=cursor)
        features = self._search_features(search_def)

        field_toggles = self._search_field_toggles(search_def)
        params = {
            "variables": self._compact_json(variables),
            "features": self._compact_json(features),
        }
        if isinstance(field_toggles, dict) and field_toggles:
            params["fieldToggles"] = self._compact_json(field_toggles)

        if self.dry_run:
            print("    [DRY-RUN] Request params prepared")
            print(f"      variables={variables}")
            return {"_dry_run": True}

        response = None
        request_headers = {"referer": search_url, "x-twitter-active-user": "yes"}
        for attempt in range(max_retries):
            response = self.api_manager.make_request(
                endpoint,
                url,
                params=params,
                max_retries=1,
                headers=request_headers,
            )
            if response:
                break
            if attempt < max_retries - 1:
                self._random_human_pause("search_retry")

        if not response:
            return None

        try:
            return response.json()
        except Exception as exc:
            self.storage_manager.log_event("fetch_failures", f"SearchTimeline JSON parse error: {exc}")
            return None

    def _run_single_search_fetch(
        self,
        search_def: Dict,
        raw_query: str,
        search_url: str,
        product: str,
        policy: Dict,
        paths: Dict[str, Path],
    ) -> Dict:
        query_id = self.api_manager.get_query_id("SearchTimeline")
        if not query_id:
            raise RuntimeError("Missing search_timeline_query_id in config.json -> api_config")
        static_tx_id = str(self.api_manager.session.headers.get("x-client-transaction-id", "")).strip()
        print(f"    Static query-id: {query_id}")
        print(f"    Static tx-id: {static_tx_id or 'missing'}")

        max_pages = int(policy["pagination_depth"])
        max_retries = int(policy["max_retries"])
        warmup_seconds = int(self.config.get("api_config", {}).get("search_warmup_seconds", 2))

        all_tweets: List[Dict] = []
        seen_ids: Set[str] = set()
        cursor: Optional[str] = None
        page = 1
        progress_state = self.fetcher._build_timeline_progress_state()
        raw_files: List[Path] = []
        exhausted_reason: Optional[str] = None
        cursor_history: List[str] = []
        warmup_status: Optional[int] = None
        first_request_status: Optional[int] = None
        first_page_debug: Dict = {
            "entry_type_counts": {},
            "cursor_candidates": [],
            "skipped_entries": [],
            "processed_entries": [],
            "selected_cursor_source": None,
            "skipped_entries_total": 0,
        }

        oldest_seen: Optional[datetime] = None
        newest_seen: Optional[datetime] = None

        while page <= max_pages:
            if cursor and not self.fetcher._can_use_cursor(progress_state, cursor):
                exhausted_reason = "cursor_attempt_budget_exhausted"
                self.storage_manager.log_event(
                    "cursor_blacklist",
                    f"SearchTimeline[{search_def.get('name', 'unknown')}]: cursor blacklisted; cursor={cursor}",
                )
                break

            if cursor is None and not self.dry_run:
                warmup_ok = False
                try:
                    warmup_resp = self.api_manager.session.get(search_url, timeout=30)
                    warmup_status = int(warmup_resp.status_code)
                    warmup_ok = 200 <= warmup_resp.status_code < 400
                    print(
                        f"    Warmup status: {warmup_resp.status_code} "
                        f"(url={search_url})"
                    )
                except Exception as exc:
                    print(f"    Warmup status: exception ({exc})")
                    self.storage_manager.log_event(
                        "fetch_failures",
                        f"SearchTimeline[{search_def.get('name', 'unknown')}]: warmup exception {exc}",
                    )
                if not warmup_ok:
                    self.storage_manager.log_event(
                        "endpoint_health",
                        f"SearchTimeline[{search_def.get('name', 'unknown')}]: warmup failed before first request",
                    )
                if warmup_seconds > 0:
                    time.sleep(warmup_seconds)

            response_json = self._request_search_timeline_page(
                query_id=query_id,
                raw_query=raw_query,
                search_url=search_url,
                search_def=search_def,
                product=product,
                cursor=cursor,
                max_retries=max_retries,
            )

            if not response_json:
                if cursor and self.api_manager.get_last_status("SearchTimeline") == 404:
                    self.storage_manager.log_event(
                        "cursor_exhausted",
                        f"SearchTimeline[{search_def.get('name', 'unknown')}]: dead cursor 404 | cursor={cursor}",
                    )
                    if self.fetcher._should_try_single_reseed(progress_state, cursor):
                        self.storage_manager.log_event(
                            "cursor_recovery",
                            f"SearchTimeline[{search_def.get('name', 'unknown')}]: single reseed after cursor 404",
                        )
                        cursor = None
                        self._random_human_pause("search_retry")
                        continue
                    exhausted_reason = "dead_cursor_404"
                    break

                health = self.api_manager.get_endpoint_health("SearchTimeline")
                last_status = self.api_manager.get_last_status("SearchTimeline")
                self.storage_manager.log_event(
                    "endpoint_health",
                    f"SearchTimeline[{search_def.get('name', 'unknown')}]: request failed | health={health} | status={last_status}",
                )
                exhausted_reason = f"request_failed_{health}"
                if page == 1:
                    print(
                        f"    First request failed before any page parse. "
                        f"status={last_status} health={health}"
                    )
                    first_request_status = last_status
                break

            if not self.dry_run:
                raw_file = self._save_raw_payload(paths["root"], str(paths["safe_name"]), page, response_json)
                raw_files.append(raw_file)

            instructions = self._extract_search_instructions(response_json)
            page_result = self._parse_search_page(
                instructions=instructions,
                seen_ids=seen_ids,
                capture_debug=(page == 1),
            )
            page_tweets = page_result["tweets"]
            all_tweets.extend(page_tweets)
            if page == 1:
                first_request_status = self.api_manager.get_last_status("SearchTimeline")
                first_page_debug = {
                    "entry_type_counts": page_result.get("entry_type_counts", {}),
                    "cursor_candidates": page_result.get("cursor_candidates", []),
                    "skipped_entries": page_result.get("skipped_entries", []),
                    "processed_entries": page_result.get("processed_entries", []),
                    "selected_cursor_source": page_result.get("selected_cursor_source"),
                    "skipped_entries_total": len(page_result.get("skipped_entries", [])),
                }

            if page_result["oldest_dt"] and (oldest_seen is None or page_result["oldest_dt"] < oldest_seen):
                oldest_seen = page_result["oldest_dt"]
            if page_result["newest_dt"] and (newest_seen is None or page_result["newest_dt"] > newest_seen):
                newest_seen = page_result["newest_dt"]

            next_cursor = page_result["next_cursor"]
            new_items_on_page = len(page_tweets)

            stall_reason = self.fetcher._classify_stall_reason(
                cursor=cursor,
                next_cursor=next_cursor,
                has_entries=bool(page_result["has_entries"]),
                timeline_item_count=int(page_result["timeline_item_count"]),
                timeline_module_count=int(page_result["timeline_module_count"]),
                new_items_count=new_items_on_page,
            )
            if cursor and next_cursor and next_cursor in progress_state["seen_cursor_values"]:
                stall_reason = "repeated_cursor_history"

            if new_items_on_page > 0:
                progress_state["had_progress_before_stall"] = True
                progress_state["no_progress_pages"] = 0
            else:
                progress_state["no_progress_pages"] += 1

            print(
                f"    Page {page}: new={new_items_on_page} total={len(all_tweets)} "
                f"cursor={str(cursor)[:24] if cursor else 'none'} -> {str(next_cursor)[:24] if next_cursor else 'none'}"
            )
            if page == 1:
                first_status = self.api_manager.get_last_status("SearchTimeline")
                print(f"      First SearchTimeline request status: {first_status}")
                selected_source = page_result.get("selected_cursor_source")
                if selected_source:
                    print(f"      Cursor discovery path: {selected_source}")
            if newest_seen or oldest_seen:
                newest_label = newest_seen.isoformat() if newest_seen else "unknown"
                oldest_label = oldest_seen.isoformat() if oldest_seen else "unknown"
                print(f"      timestamps newest={newest_label} | oldest={oldest_label}")
            if next_cursor:
                print(f"      Cursor extracted: yes ({str(next_cursor)[:48]}...)")
            else:
                print("      Cursor extracted: no")

            if stall_reason:
                if "repeated_cursor" in stall_reason:
                    self.storage_manager.log_event(
                        "repeated_cursor_detected",
                        f"SearchTimeline[{search_def.get('name', 'unknown')}]: {stall_reason} | cursor={cursor or 'none'} | next={next_cursor or 'none'}",
                    )
                if "no_new_tweets" in stall_reason:
                    self.storage_manager.log_event(
                        "no_new_tweets_detected",
                        f"SearchTimeline[{search_def.get('name', 'unknown')}]: no new tweets on cursor page | cursor={cursor or 'none'}",
                    )
                self.storage_manager.log_event(
                    "cursor_exhausted",
                    f"SearchTimeline[{search_def.get('name', 'unknown')}]: {stall_reason} | cursor={cursor or 'none'} | next={next_cursor or 'none'}",
                )
                if self.fetcher._should_try_single_reseed(progress_state, cursor):
                    self.storage_manager.log_event(
                        "cursor_recovery",
                        f"SearchTimeline[{search_def.get('name', 'unknown')}]: single reseed after {stall_reason}",
                    )
                    cursor = None
                    self._random_human_pause("search_retry")
                    continue
                exhausted_reason = stall_reason
                break

            if not next_cursor:
                exhausted_reason = "no_bottom_cursor"
                self.storage_manager.log_event(
                    "pagination_terminated",
                    f"SearchTimeline[{search_def.get('name', 'unknown')}]: no further bottom cursor; terminated cleanly",
                )
                break

            progress_state["seen_cursor_values"].add(str(next_cursor))
            progress_state["cursor_generation_history"].append(str(next_cursor))
            cursor_history.append(str(next_cursor))
            self.storage_manager.log_event(
                "cursor_accepted",
                f"SearchTimeline[{search_def.get('name', 'unknown')}]: cursor accepted {str(next_cursor)[:36]}...",
            )

            cursor = next_cursor
            page += 1
            self._random_human_pause("between_pages")

        if page > max_pages and exhausted_reason is None:
            exhausted_reason = "pagination_depth_limit_reached"

        return {
            "tweets": all_tweets,
            "raw_files": raw_files,
            "pages_fetched": page if all_tweets else max(1, page - 1),
            "exhausted_reason": exhausted_reason or "unknown",
            "oldest_seen": oldest_seen,
            "newest_seen": newest_seen,
            "cursor_history": cursor_history,
            "warmup_status": warmup_status,
            "first_request_status": first_request_status,
            "first_page_debug": first_page_debug,
        }

    def monitor_search(self, search_def: Dict) -> bool:
        name = str(search_def.get("name", "search_timeline")).strip() or "search_timeline"
        key = name.lower()
        policy = self._policy_for_search(search_def)
        product = self._normalize_product(search_def)

        now_dt = datetime.now(self.tz)
        raw_query = SearchQueryBuilder.build_raw_query(search_def, now_dt)
        if not raw_query:
            self.storage_manager.log_event("fetch_failures", f"SearchTimeline[{name}]: raw query is empty")
            print(f"  ✗ Skipping {name}: empty generated rawQuery")
            return False

        search_url = SearchQueryBuilder.build_human_search_url(raw_query, product)
        paths = self._search_paths(search_def, product)
        rate_limit = self._rate_limit_state()

        print(f"\n{SEP}")
        print(f"🔎 SearchTimeline Monitor: {name}")
        print(SEP)
        print(f"  URL: {search_url}")
        print(f"  rawQuery: {raw_query}")
        print(f"  Product: {product}")
        print(f"  Poll interval: {policy['poll_interval_seconds']}s")
        print(f"  Pagination depth: {policy['pagination_depth']}")
        print("  Dedup mode: in-run tweet-id overwrite")
        print(
            "  Rate limit state: "
            f"remaining={rate_limit.get('remaining', 'unknown')} "
            f"limit={rate_limit.get('limit', 'unknown')} "
            f"reset={rate_limit.get('reset', 'unknown')}"
        )

        if self.dry_run:
            self.last_fetch[key] = datetime.now(self.tz)
            print("  [DRY-RUN] Skipping network + storage writes")
            return True

        try:
            result = self._run_single_search_fetch(
                search_def=search_def,
                raw_query=raw_query,
                search_url=search_url,
                product=product,
                policy=policy,
                paths=paths,
            )
        except Exception as exc:
            self.error_count[key] += 1
            self.storage_manager.log_event("fetch_failures", f"SearchTimeline[{name}] fatal error: {exc}")
            print(f"  ✗ Fatal fetch error: {exc}")
            return False

        run_metadata = {
            "search_url": search_url,
            "pages_fetched": int(result["pages_fetched"]),
            "cursor_history": result.get("cursor_history", []),
            "cursor_exhaustion_reason": result["exhausted_reason"],
            "warmup_status": result.get("warmup_status"),
            "first_request_status": result.get("first_request_status"),
        }
        persist_result = self._persist_run_exports(
            search_def=search_def,
            product=product,
            raw_query=raw_query,
            paths=paths,
            fetched_tweets=result["tweets"],
            run_metadata=run_metadata,
            parser_debug=result.get("first_page_debug", {}),
        )

        self.last_fetch[key] = datetime.now(self.tz)
        self.error_count[key] = 0

        print(f"  Cursor exhaustion: {result['exhausted_reason']}")
        print(f"  Pages fetched: {result['pages_fetched']}")
        print(f"  Saved tweets total: {persist_result['current_count']}")
        print(f"  New tweets: {persist_result['new_count']}")
        print(f"  Duplicate overwrites: {persist_result['duplicate_updates']}")
        if persist_result["newest_dt"] or persist_result["oldest_dt"]:
            newest_label = persist_result["newest_dt"].isoformat() if persist_result["newest_dt"] else "unknown"
            oldest_label = persist_result["oldest_dt"].isoformat() if persist_result["oldest_dt"] else "unknown"
            print(f"  Extracted timestamps newest={newest_label} oldest={oldest_label}")
        print(f"  Parsed TXT: {persist_result['output_txt']}")
        print(f"  Parsed JSON: {persist_result['output_json']}")
        print(f"  Debug prefix: {persist_result['debug_prefix']}")
        print(f"  Raw pages stored: {len(result['raw_files'])}")
        return True

    def run_cycle(self, only_names: Optional[Set[str]] = None):
        print(f"\n{SEP}")
        print(f"SEARCH MONITOR CYCLE - {self.storage_manager.get_jalali_datetime()}")
        print(SEP)

        enabled = []
        for item in self.search_definitions:
            if not item.get("enabled", True):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            if only_names and name.lower() not in only_names:
                continue
            enabled.append(item)

        if not enabled:
            print("No enabled search definitions to process.")
            return

        enabled.sort(key=lambda cfg: int(cfg.get("polling_priority", 4)))

        due: List[Tuple[Dict, Dict]] = []
        for search_def in enabled:
            name = str(search_def.get("name", "")).strip()
            key = name.lower()
            policy = self._policy_for_search(search_def)
            interval = int(policy["poll_interval_seconds"])
            if self._should_fetch_search(key, interval):
                due.append((search_def, policy))

        if not due:
            print("No search definitions are due this cycle.")
            return

        print(f"Processing {len(due)} due search definition(s)...")

        success = 0
        failed = 0
        for index, (search_def, _policy) in enumerate(due, start=1):
            name = str(search_def.get("name", "search_timeline"))
            print(f"\n[{index}/{len(due)}] {name}")
            print("-" * 70)
            ok = self.monitor_search(search_def)
            if ok:
                success += 1
            else:
                failed += 1
            if index < len(due):
                self._random_human_pause("between_searches")

        stats = self.api_manager.get_stats()
        print(f"\n{SEP}")
        print("CYCLE COMPLETE")
        print(SEP)
        print(f"✅ Successful: {success}")
        print(f"⚠️  Failed: {failed}")
        print(f"📊 Requests made: {stats['requests_made']}")
        print(f"📊 Requests/min: {stats['requests_per_minute']}")

    def run_continuous(self, check_interval: int = 60, only_names: Optional[Set[str]] = None):
        print(f"\n{SEP}")
        print("STARTING SEARCHTIMELINE CONTINUOUS MONITORING")
        print(SEP)
        print("Press Ctrl+C to stop.\n")

        cycle = 0
        try:
            while True:
                cycle += 1
                self.run_cycle(only_names=only_names)
                extra_pause = 0
                sim = self.config.get("anti_bot_simulation", {})
                if sim.get("enabled", True):
                    delays = sim.get("delays_seconds", {})
                    min_d = float(delays.get("between_cycles_min", 0))
                    max_d = float(delays.get("between_cycles_max", 0))
                    if max_d < min_d:
                        max_d = min_d
                    if max_d > 0:
                        extra_pause = random.uniform(min_d, max_d)
                total_wait = check_interval + int(extra_pause)
                print(f"\n⏸️  Waiting {total_wait}s before next cycle check...")
                time.sleep(total_wait)
        except KeyboardInterrupt:
            print(f"\n\n{SEP}")
            print("SEARCH MONITOR STOPPED")
            print(SEP)
            print(f"Cycles completed: {cycle}")
            stats = self.api_manager.get_stats()
            print(f"Total requests: {stats['requests_made']}")
            print(f"Session duration: {stats['session_duration_seconds']}s")
            print("Stopped gracefully.")

    def validate_with_reference_file(
        self,
        reference_file: str,
        search_name: Optional[str] = None,
    ) -> bool:
        ref_path = Path(reference_file)
        if not ref_path.is_absolute():
            ref_path = self.base_dir / ref_path
        if not ref_path.exists():
            print(f"✗ Reference file not found: {ref_path}")
            return False

        target_def = None
        for entry in self.search_definitions:
            if not entry.get("enabled", True):
                continue
            if search_name and str(entry.get("name", "")).lower() != search_name.lower():
                continue
            target_def = entry
            break
        if target_def is None:
            print("✗ No suitable enabled search definition found for validation.")
            return False

        with open(ref_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        page_result = self._parse_search_page(
            instructions=self._extract_search_instructions(payload),
            seen_ids=set(),
            capture_debug=True,
        )

        tweets = page_result["tweets"]
        media_items = 0
        for tweet in tweets:
            media = tweet.get("media", {}) if isinstance(tweet.get("media"), dict) else {}
            media_items += len(media.get("image_urls", []))
            media_items += len(media.get("video_urls", []))
            media_items += len(media.get("m3u8_urls", []))
            media_items += len(media.get("quoted_image_urls", []))
            media_items += len(media.get("quoted_video_urls", []))
            media_items += len(media.get("quoted_m3u8_urls", []))
            media_items += len(media.get("card_urls", []))
            media_items += len(media.get("external_urls", []))

        print(f"\n{SEP}")
        print("REFERENCE VALIDATION")
        print(SEP)
        print(f"Reference file: {ref_path}")
        print(f"Parsed tweets: {len(tweets)}")
        print(f"Bottom cursor: {page_result['next_cursor']}")
        print(f"Timeline items: {page_result['timeline_item_count']}")
        print(f"Timeline modules: {page_result['timeline_module_count']}")
        print(f"Extracted media/url entries: {media_items}")
        return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SearchTimeline monitor")
    parser.add_argument("--config", default="config.json", help="Main config path")
    parser.add_argument("--search-config", default="search_config.json", help="Search config path")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--check-interval", type=int, default=60, help="Cycle check interval (seconds)")
    parser.add_argument("--name", action="append", help="Run only this search name (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="Build requests/config only, no API/storage writes")
    parser.add_argument(
        "--validate-reference",
        help="Parse a local SearchTimeline JSON payload (e.g., REFERENCE_FILES/SearchTimeline.txt)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print(SEP)
    print("Twitter SearchTimeline Rolling Monitor")
    print(SEP)
    print("\nUsing existing architecture:")
    print("  - api_manager.py for networking/session/rate-limit")
    print("  - storage_manager.py for storage/logging/dedupe registry")
    print("  - fetch_historical_tweets_hybrid.py parser helpers")
    print("  - SearchTimeline deterministic pagination + dedupe\n")

    monitor = SearchTimelineMonitor(
        config_path=args.config,
        search_config_path=args.search_config,
        dry_run=args.dry_run,
    )

    selected_names = {name.lower() for name in (args.name or []) if str(name).strip()}

    if args.validate_reference:
        ok = monitor.validate_with_reference_file(args.validate_reference, search_name=(args.name[0] if args.name else None))
        if not ok:
            sys.exit(1)
        if args.once:
            return

    if args.once:
        monitor.run_cycle(only_names=selected_names or None)
    else:
        monitor.run_continuous(check_interval=max(1, args.check_interval), only_names=selected_names or None)


if __name__ == "__main__":
    main()
