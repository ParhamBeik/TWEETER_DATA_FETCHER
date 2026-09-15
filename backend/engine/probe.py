#!/usr/bin/env python3
"""Read-only diagnostic walk of one account's profile timeline.

The archive walk records *its own verdict* about why it stopped, and that verdict
is the thing under suspicion: 61 accounts are parked as "X's serving depth" with
no way to tell whether X really refused to page deeper or the walk gave up early.
State cannot answer that question, because state is what is in doubt. This asks X.

It pages `UserTweets` with every stopping heuristic removed -- no rolling window,
no empty-page streak, no completion flags -- and prints what each page actually
contained. Nothing durable is written: the caller runs it against a scratch
PROJECT_ROOT and throws the directory away, so a probe can never mark an archive
complete or move a live cursor.

Run (via the Django command, which builds the scratch root and session):
    python -m engine.probe --account elonmusk --pages 60
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from typing import Any, Dict, List, Optional

from engine.processing import TweetSetProcessor, tweet_datetime
from engine.timeline import FetcherEngine


ENDPOINT = "UserTweets"


def _page_entry_count(payload: Dict[str, Any]) -> int:
    try:
        instructions = payload["data"]["user"]["result"]["timeline"]["timeline"]["instructions"]
    except (KeyError, TypeError):
        return 0
    return sum(
        len(instruction.get("entries", []) or [])
        for instruction in instructions
        if isinstance(instruction, dict)
    )


def _entry_kinds(payload: Dict[str, Any]) -> Counter:
    """What the entries on a page actually are.

    A page can carry a full complement of entries and still extract zero tweets.
    Whether that is X serving cursors and separators or the extractor dropping
    real posts is the difference between a harmless empty page and silent data
    loss, and only the entry types can tell them apart.
    """
    kinds: Counter = Counter()
    try:
        instructions = payload["data"]["user"]["result"]["timeline"]["timeline"]["instructions"]
    except (KeyError, TypeError):
        return kinds
    for instruction in instructions:
        if not isinstance(instruction, dict):
            continue
        for entry in instruction.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            content = entry.get("content") or {}
            kind = content.get("entryType") or content.get("__typename") or "unknown"
            item_type = (content.get("itemContent") or {}).get("itemType")
            kinds[f"{kind}:{item_type}" if item_type else str(kind)] += 1
    return kinds


def _tweet_dates(tweets: List[Dict[str, Any]]) -> tuple[Optional[str], Optional[str]]:
    stamps = [dt for dt in (tweet_datetime(tweet) for tweet in tweets) if dt is not None]
    if not stamps:
        return None, None
    return min(stamps).strftime("%Y-%m-%d"), max(stamps).strftime("%Y-%m-%d")


def walk(account: str, max_pages: int, min_remaining: int) -> Dict[str, Any]:
    engine = FetcherEngine(subsystem="live")
    processor = TweetSetProcessor()
    user_id = engine._get_user_id(account)

    query_id = engine.api_manager.get_query_id(ENDPOINT)
    if not query_id:
        raise RuntimeError(f"missing query id for {ENDPOINT}")
    features = engine._timeline_features(ENDPOINT)
    field_toggles = engine._timeline_field_toggles(ENDPOINT)
    headers = {"referer": f"https://x.com/{account}", "x-twitter-active-user": "yes"}

    engine.api_manager.warmup_navigation_context(username=account, endpoint=ENDPOINT)

    rows: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    cursor: Optional[str] = None
    stop_reason = "page_budget_exhausted"

    print(f"probe @{account} user_id={user_id} pages<={max_pages}", flush=True)
    print(f"{'page':>4} {'http':>5} {'entries':>7} {'tweets':>6} {'new':>5}  {'oldest':<10} {'newest':<10} cursor", flush=True)

    for page in range(1, max_pages + 1):
        if min_remaining and engine.api_manager.remaining_requests(ENDPOINT, min_remaining) <= 0:
            stop_reason = "quota_floor_reached"
            break

        url = engine._build_graphql_url(
            endpoint=ENDPOINT,
            query_id=query_id,
            variables=engine._timeline_variables(ENDPOINT, user_id, cursor),
            features=features,
            field_toggles=field_toggles,
        )
        response = engine.api_manager.perform_get(
            endpoint=ENDPOINT, url=url, max_retries=1, username=account, headers=headers
        )
        if response.status_code != 200:
            stop_reason = f"http_{response.status_code}"
            print(f"{page:>4} {response.status_code:>5}  -- stopping", flush=True)
            break
        payload = response.json()

        extracted = processor.extract_tweets_from_raw(
            [payload], username=account, source_endpoint=ENDPOINT
        )
        tweets = list(extracted.values())
        oldest, newest = _tweet_dates(tweets)
        # The extractor already keys by author:rest_id -- reusing its keys is the
        # only spelling guaranteed to match how the pipeline dedupes.
        ids = set(extracted)
        fresh = len(ids - seen_ids)
        seen_ids |= ids
        next_cursor = engine._extract_bottom_cursor(payload)
        kinds = _entry_kinds(payload)

        rows.append({
            "page": page,
            "entries": _page_entry_count(payload),
            "tweets": len(tweets),
            "new": fresh,
            "oldest": oldest,
            "newest": newest,
            "cursor": bool(next_cursor),
        })
        print(
            f"{page:>4} {response.status_code:>5} {_page_entry_count(payload):>7} "
            f"{len(tweets):>6} {fresh:>5}  {oldest or '-':<10} {newest or '-':<10} "
            f"{'yes' if next_cursor else 'END'}",
            flush=True,
        )
        if not tweets:
            print(f"       entry kinds: {dict(kinds)}", flush=True)

        if not next_cursor:
            stop_reason = "no_cursor_offered"
            break
        cursor = next_cursor
        engine.api_manager.human_delay("between_pages")

    tweet_pages = [row for row in rows if row["tweets"]]
    empty_runs: List[int] = []
    streak = 0
    for row in rows:
        if row["tweets"]:
            if streak:
                empty_runs.append(streak)
            streak = 0
        else:
            streak += 1
    if streak:
        empty_runs.append(streak)

    summary = {
        "account": account,
        "stop_reason": stop_reason,
        "pages_fetched": len(rows),
        "pages_with_tweets": len(tweet_pages),
        "distinct_tweets": len(seen_ids),
        # The number that decides whether EMPTY_PAGE_STREAK=2 is safe: an empty
        # run that is followed by more tweets proves a tweet-less page is not the
        # end of the timeline.
        "empty_runs": empty_runs,
        "empty_runs_followed_by_tweets": [
            run for index, run in enumerate(empty_runs)
            if not (index == len(empty_runs) - 1 and not rows[-1]["tweets"])
        ],
        "newest_seen": max((row["newest"] for row in tweet_pages if row["newest"]), default=None),
        "oldest_seen": min((row["oldest"] for row in tweet_pages if row["oldest"]), default=None),
    }
    print("\nSUMMARY " + json.dumps(summary), flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only timeline depth probe.")
    parser.add_argument("--account", required=True)
    parser.add_argument("--pages", type=int, default=20)
    parser.add_argument("--min-remaining", type=int, default=10)
    args = parser.parse_args()
    walk(args.account.strip().lstrip("@"), max(1, args.pages), max(0, args.min_remaining))


if __name__ == "__main__":
    main()
