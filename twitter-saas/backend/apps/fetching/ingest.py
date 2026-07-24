"""Upsert normalized fetcher tweet dicts into Postgres."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from apps.tweets.models import Search, SearchResult, Tweet, TwitterUser


def dedup_key(item: dict) -> str:
    """Mirror StorageManager.merge_processed_items: author_id:tweet_id."""
    tweet_id = str(item.get("rest_id") or item.get("id") or item.get("tweet_id") or "").strip()
    author_id = str(item.get("author_id") or "").strip()
    if tweet_id and author_id:
        return f"{author_id}:{tweet_id}"
    return tweet_id


def _parse_created_at(value) -> datetime | None:
    if not value:
        return None
    # X uses RFC-2822 style: "Wed Oct 10 20:19:24 +0000 2018".
    try:
        dt = parsedate_to_datetime(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def upsert_tweet(item: dict) -> Tweet | None:
    key = dedup_key(item)
    if not key:
        return None
    tweet_id = str(item.get("rest_id") or item.get("id") or item.get("tweet_id") or "")
    account = str(item.get("account") or "unknown").lstrip("@")

    author = None
    if account and account != "unknown":
        author, _ = TwitterUser.objects.get_or_create(
            handle=account,
            defaults={"rest_id": item.get("author_id")},
        )

    defaults = {
        "tweet_id": tweet_id,
        "author_rest_id": item.get("author_id"),
        "account": account,
        "author": author,
        "text": item.get("text") or "",
        "url": item.get("url") or "",
        "type": item.get("type") or "Tweet",
        "created_at": _parse_created_at(item.get("created_at") or item.get("raw_timestamp")),
        "raw_created_at": str(item.get("created_at") or ""),
        "likes": item.get("likes") or 0,
        "retweets": item.get("retweets") or 0,
        "replies": item.get("replies") or 0,
        "quotes": item.get("quotes") or 0,
        "bookmarks": item.get("bookmarks") or 0,
        "views": item.get("views") or 0,
        "source_language": item.get("source_language"),
        "source_endpoint": item.get("source_endpoint") or "",
        "conversation_id": item.get("conversation_id"),
        "entities": item.get("entities") or {},
        "payload": item,
    }
    tweet, _ = Tweet.objects.update_or_create(dedup_key=key, defaults=defaults)
    return tweet


def ingest_tweets(items) -> int:
    count = 0
    for item in items:
        if upsert_tweet(item) is not None:
            count += 1
    return count


def ingest_search_results(search: Search, items) -> int:
    """Upsert tweets and link them to a search, ranked by arrival order."""
    count = 0
    for rank, item in enumerate(items):
        tweet = upsert_tweet(item)
        if tweet is None:
            continue
        SearchResult.objects.update_or_create(
            search=search, tweet=tweet, defaults={"rank": rank}
        )
        count += 1
    return count
