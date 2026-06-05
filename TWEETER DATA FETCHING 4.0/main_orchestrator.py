#!/usr/bin/env python3
"""Main entry point. Coordinates tier configs, session checks, fetching, and data pipelines."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from core.fetcher_engine import FetcherEngine
from core.set_operations import TweetSetProcessor
from data_pipeline.storage_manager import StorageManager
from exporters.text_export_helper import export_tweets_to_txt


def _endpoint_to_raw_folder(endpoint: str) -> str:
    mapping = {
        "UserTweets": "UserTweets",
        "UserTweetsAndReplies": "UserTweetsAndReplies",
    }
    return mapping[endpoint]


def run_phase3() -> None:
    project_root = Path(__file__).resolve().parent
    engine = FetcherEngine(config_path="config/config.json")
    storage = StorageManager(project_root=project_root)
    processor = TweetSetProcessor()

    accounts = engine.account_map and sorted(
        {meta.get("username", "").strip() for meta in engine.account_map.values() if meta.get("username")}
    ) or []

    for username in accounts:
        storage.ensure_account_state(username)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        user_id = engine._get_user_id(username)
        policy = engine.priority_policies.get(
            engine.account_map.get(username.lower(), {}).get("priority", 7),
            {"historical_max_pages": 15},
        )
        max_pages = int(policy.get("historical_max_pages", 15))

        raw_pages_a: List[Dict[str, Any]] = engine._fetch_endpoint_pages(
            account=username,
            user_id=user_id,
            endpoint="UserTweets",
            max_pages=max_pages,
        )
        storage.save_raw_json(raw_pages_a, _endpoint_to_raw_folder("UserTweets"), username, ts)

        raw_pages_b: List[Dict[str, Any]] = engine._fetch_endpoint_pages(
            account=username,
            user_id=user_id,
            endpoint="UserTweetsAndReplies",
            max_pages=max_pages,
        )
        storage.save_raw_json(raw_pages_b, _endpoint_to_raw_folder("UserTweetsAndReplies"), username, ts)

        set_a = processor.extract_tweets_from_raw(raw_pages_a)
        set_b = processor.extract_tweets_from_raw(raw_pages_b)

        list_a = list(set_a.values())
        list_b = list(set_b.values())
        list_intersection = processor.get_intersection(set_a, set_b)
        list_union = processor.get_union(set_a, set_b)
        list_replies_only = processor.get_difference_b_minus_a(set_a, set_b)

        out_a = storage.save_processed_set(list_a, "A", username)
        out_b = storage.save_processed_set(list_b, "B", username)
        out_intersection = storage.save_processed_set(list_intersection, "INTERSECTION", username)
        out_union = storage.save_processed_set(list_union, "UNION", username)
        out_replies_only = storage.save_processed_set(list_replies_only, "REPLIES_ONLY", username)

        export_tweets_to_txt(
            list_union,
            str(out_union.with_suffix(".txt")),
        )
        export_tweets_to_txt(
            list_replies_only,
            str(out_replies_only.with_suffix(".txt")),
        )

        print(
            f"[PHASE3] @{username} complete | "
            f"A={len(list_a)} B={len(list_b)} "
            f"∩={len(list_intersection)} ∪={len(list_union)} B-A={len(list_replies_only)}"
        )


if __name__ == "__main__":
    run_phase3()
