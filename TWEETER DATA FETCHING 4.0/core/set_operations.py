#!/usr/bin/env python3
"""
Tweet set extraction and mathematical set operations.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class TweetSetProcessor:
    """Parse raw pages and compute A/B/union/intersection/difference sets."""

    def extract_tweets_from_raw(self, raw_pages: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """
        Extract tweets from GraphQL timeline instructions.
        Returns dict[tweet_key] = tweet_object for inherent deduplication.
        """
        result: Dict[str, Dict[str, Any]] = {}
        if not isinstance(raw_pages, list):
            return result

        for page in raw_pages:
            instructions = (
                page.get("data", {})
                .get("user", {})
                .get("result", {})
                .get("timeline", {})
                .get("timeline", {})
                .get("instructions", [])
            )
            if not isinstance(instructions, list):
                continue

            for inst in instructions:
                if not isinstance(inst, dict):
                    continue
                if inst.get("type") != "TimelineAddEntries":
                    continue
                entries = inst.get("entries", [])
                if not isinstance(entries, list):
                    continue

                for entry in entries:
                    if not isinstance(entry, dict):
                        continue

                    tweet_candidate = self._extract_tweet_from_entry(entry)
                    if not tweet_candidate:
                        continue

                    key = self._tweet_key(tweet_candidate)
                    if key:
                        result[key] = tweet_candidate

                    content = entry.get("content", {})
                    if isinstance(content, dict):
                        for module_item in content.get("items", []) if isinstance(content.get("items"), list) else []:
                            if not isinstance(module_item, dict):
                                continue
                            item = module_item.get("item", {})
                            if not isinstance(item, dict):
                                continue
                            module_tweet = self._extract_tweet_from_item(item)
                            if not module_tweet:
                                continue
                            module_key = self._tweet_key(module_tweet)
                            if module_key:
                                result[module_key] = module_tweet

        return result

    def _extract_tweet_from_entry(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        content = entry.get("content", {})
        if not isinstance(content, dict):
            return None
        item_content = content.get("itemContent", {})
        if not isinstance(item_content, dict):
            return None
        return self._extract_tweet_from_item(item_content)

    def _extract_tweet_from_item(self, item_content: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        tweet_results = item_content.get("tweet_results", {})
        if not isinstance(tweet_results, dict):
            return None
        tweet_obj = tweet_results.get("result")
        if not isinstance(tweet_obj, dict):
            return None

        legacy = tweet_obj.get("legacy", {})
        if not isinstance(legacy, dict):
            return None
        if not tweet_obj.get("rest_id"):
            return None
        return tweet_obj

    @staticmethod
    def _tweet_key(tweet_obj: Dict[str, Any]) -> Optional[str]:
        tweet_id = tweet_obj.get("rest_id")
        if not tweet_id:
            return None
        author_id = (
            tweet_obj.get("core", {})
            .get("user_results", {})
            .get("result", {})
            .get("rest_id")
        )
        if author_id:
            return f"{author_id}:{tweet_id}"
        return str(tweet_id)

    def get_union(self, set_a: Dict[str, Dict[str, Any]], set_b: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged = dict(set_a or {})
        merged.update(set_b or {})
        return list(merged.values())

    def get_intersection(self, set_a: Dict[str, Dict[str, Any]], set_b: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        keys = set((set_a or {}).keys()) & set((set_b or {}).keys())
        return [set_b[k] if k in set_b else set_a[k] for k in keys]

    def get_difference_b_minus_a(self, set_a: Dict[str, Dict[str, Any]], set_b: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        keys = set((set_b or {}).keys()) - set((set_a or {}).keys())
        return [set_b[k] for k in keys if k in set_b]

