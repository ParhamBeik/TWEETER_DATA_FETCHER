#!/usr/bin/env python3
"""
Diagnose why UserTweetsAndReplies minus UserTweets is empty for an account.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.core.set_operations import TweetSetProcessor
from shared.data_pipeline.storage_manager import StorageManager


def key_by_id(tweet: Dict[str, Any]) -> str:
    return str(tweet.get("id") or tweet.get("rest_id") or "")


def describe_set(name: str, tweets: Dict[str, Dict[str, Any]]) -> None:
    type_counts = Counter(tweet.get("type") for tweet in tweets.values())
    reply_flags = sum(1 for tweet in tweets.values() if tweet.get("in_reply_to_status_id"))
    accounts = Counter(str(tweet.get("account", "unknown")).lower() for tweet in tweets.values())
    print(
        f"{name}: count={len(tweets)} "
        f"types={dict(type_counts)} reply_flags={reply_flags} "
        f"top_accounts={accounts.most_common(5)}"
    )


def load_pages(storage: StorageManager, username: str, endpoint: str) -> list[dict]:
    state = storage.get_endpoint_state(username, endpoint)
    batch = state.get("raw_batch_path")
    if batch:
        pages = storage.load_raw_pages_from_batch(batch)
        if pages:
            return pages
    return storage.load_all_raw_pages(endpoint, username, include_legacy=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose UserTweetsAndReplies minus UserTweets behavior.")
    parser.add_argument("username", help="Account username, with or without @")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--samples", type=int, default=10)
    args = parser.parse_args()

    username = args.username.strip().lstrip("@")
    storage = StorageManager(project_root=Path(args.project_root), subsystem="historical")
    processor = TweetSetProcessor()

    raw_a = load_pages(storage, username, "UserTweets")
    raw_b = load_pages(storage, username, "UserTweetsAndReplies")
    set_a = processor.extract_tweets_from_raw(raw_a, username=username, source_endpoint="UserTweets")
    set_b = processor.extract_tweets_from_raw(raw_b, username=username, source_endpoint="UserTweetsAndReplies")

    describe_set("A UserTweets", set_a)
    describe_set("B UserTweetsAndReplies", set_b)

    keys_a = set(set_a)
    keys_b = set(set_b)
    ids_a = {key_by_id(tweet) for tweet in set_a.values() if key_by_id(tweet)}
    ids_b = {key_by_id(tweet) for tweet in set_b.values() if key_by_id(tweet)}

    print(f"key_intersection={len(keys_a & keys_b)} key_B_minus_A={len(keys_b - keys_a)}")
    print(f"id_intersection={len(ids_a & ids_b)} id_B_minus_A={len(ids_b - ids_a)}")

    print("\nB-A samples by canonical key:")
    for key in list(keys_b - keys_a)[: max(0, args.samples)]:
        tweet = set_b[key]
        print(json.dumps({
            "key": key,
            "id": tweet.get("id"),
            "account": tweet.get("account"),
            "type": tweet.get("type"),
            "in_reply_to_status_id": tweet.get("in_reply_to_status_id"),
            "text": str(tweet.get("text", ""))[:180],
        }, ensure_ascii=False))

    if not keys_b - keys_a and not ids_b - ids_a:
        print("\nConclusion: B is a subset of A for this data. Likely API reality or endpoint/context issue, not a set-operation key mismatch.")
    elif not keys_b - keys_a and ids_b - ids_a:
        print("\nConclusion: canonical key mismatch or author-id issue should be investigated.")
    else:
        print("\nConclusion: replies-only records exist; inspect export/state path if processed output is empty.")


if __name__ == "__main__":
    main()
