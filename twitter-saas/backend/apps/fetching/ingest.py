"""Upsert normalized fetcher tweet dicts into Postgres."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

from apps.tweets.models import Search, SearchResult, Tweet, TweetMetric, TwitterUser

_EXTRAS_KEYS = (
    "media",
    "card",
    "reply_to",
    "quoted_tweet",
    "retweeted_tweet",
    "possibly_sensitive",
)

_TWEET_UPDATE_FIELDS = [
    "tweet_id",
    "author_rest_id",
    "account",
    "author",
    "text",
    "url",
    "type",
    "created_at",
    "raw_created_at",
    "likes",
    "retweets",
    "replies",
    "quotes",
    "bookmarks",
    "views",
    "source_language",
    "source_endpoint",
    "conversation_id",
    "entities",
    "extras",
    "payload",
]


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


def _extras_from_item(item: dict) -> dict:
    return {key: item.get(key) for key in _EXTRAS_KEYS}


def _author_defaults(item: dict, account: str) -> dict | None:
    if not account or account == "unknown":
        return None
    author_data = item.get("author", {}) if isinstance(item.get("author"), dict) else {}
    defaults = {"handle": account}
    rest_id = author_data.get("id") or item.get("author_id")
    if rest_id:
        defaults["rest_id"] = str(rest_id)
    display_name = author_data.get("display_name") or item.get("author_display_name")
    if display_name:
        defaults["display_name"] = display_name
    avatar_url = author_data.get("avatar_url") or item.get("author_avatar_url")
    if avatar_url:
        defaults["avatar_url"] = avatar_url
    if author_data.get("verified") is not None or item.get("author_verified") is not None:
        defaults["verified"] = bool(author_data.get("verified") or item.get("author_verified"))
    verified_type = author_data.get("verified_type") or item.get("author_verified_type")
    if verified_type:
        defaults["verified_type"] = verified_type
    return defaults


def _tweet_row(item: dict, authors_by_handle: dict[str, TwitterUser]) -> Tweet | None:
    key = dedup_key(item)
    if not key:
        return None
    tweet_id = str(item.get("rest_id") or item.get("id") or item.get("tweet_id") or "")
    author_data = item.get("author", {}) if isinstance(item.get("author"), dict) else {}
    account = str(item.get("account") or author_data.get("handle") or "unknown").lstrip("@").lower()
    # Leave NULL when the timestamp is unparseable. Stamping now() would sort the
    # tweet to the top of the feed forever, and raw_created_at preserves the
    # original string for diagnosis.
    created_at = _parse_created_at(item.get("created_at") or item.get("raw_timestamp"))
    return Tweet(
        dedup_key=key,
        tweet_id=tweet_id,
        author_rest_id=item.get("author_id"),
        account=account,
        author=authors_by_handle.get(account),
        text=item.get("text") or "",
        url=item.get("url") or "",
        type=item.get("type") or "Tweet",
        created_at=created_at,
        raw_created_at=str(item.get("created_at") or ""),
        likes=item.get("likes") or 0,
        retweets=item.get("retweets") or 0,
        replies=item.get("replies") or 0,
        quotes=item.get("quotes") or 0,
        bookmarks=item.get("bookmarks") or 0,
        views=item.get("views") or 0,
        source_language=item.get("source_language"),
        source_endpoint=item.get("source_endpoint") or "",
        conversation_id=item.get("conversation_id"),
        entities=item.get("entities") or {},
        extras=_extras_from_item(item),
        payload=item,
    )


def _upsert_authors(items: Iterable[dict]) -> dict[str, TwitterUser]:
    pending: dict[str, dict] = {}
    for item in items:
        author_data = item.get("author", {}) if isinstance(item.get("author"), dict) else {}
        account = str(item.get("account") or author_data.get("handle") or "unknown").lstrip("@").lower()
        defaults = _author_defaults(item, account)
        if defaults is None:
            continue
        existing = pending.get(account, {"handle": account})
        existing.update(defaults)
        pending[account] = existing
    if not pending:
        return {}
    known = {
        user.handle: user
        for user in TwitterUser.objects.filter(handle__in=pending.keys())
    }
    to_create: list[TwitterUser] = []
    to_update: list[TwitterUser] = []
    update_fields: set[str] = set()
    for handle, fields in pending.items():
        if handle in known:
            user = known[handle]
            for key, value in fields.items():
                if key == "handle":
                    continue
                setattr(user, key, value)
                update_fields.add(key)
            to_update.append(user)
        else:
            to_create.append(TwitterUser(**fields))
    if to_create:
        TwitterUser.objects.bulk_create(to_create, ignore_conflicts=True)
    if to_update and update_fields:
        TwitterUser.objects.bulk_update(to_update, fields=sorted(update_fields))
    return {
        user.handle: user
        for user in TwitterUser.objects.filter(handle__in=pending.keys())
    }


def upsert_tweet(item: dict) -> Tweet | None:
    authors = _upsert_authors([item])
    row = _tweet_row(item, authors)
    if row is None:
        return None
    previous = {
        t.dedup_key: t
        for t in Tweet.objects.filter(dedup_key=row.dedup_key).only(
            "dedup_key", "likes", "retweets", "views"
        )
    }
    Tweet.objects.bulk_create(
        [row],
        update_conflicts=True,
        unique_fields=["dedup_key"],
        update_fields=_TWEET_UPDATE_FIELDS,
    )
    saved = Tweet.objects.filter(dedup_key=row.dedup_key).first()
    _record_metrics([row], previous)
    return saved


def ingest_tweets(items) -> int:
    batch = [item for item in items if isinstance(item, dict)]
    if not batch:
        return 0
    authors = _upsert_authors(batch)
    rows: list[Tweet] = []
    seen: set[str] = set()
    for item in batch:
        row = _tweet_row(item, authors)
        if row is None or row.dedup_key in seen:
            continue
        seen.add(row.dedup_key)
        rows.append(row)
    if not rows:
        return 0
    previous = {
        t.dedup_key: t
        for t in Tweet.objects.filter(dedup_key__in=[r.dedup_key for r in rows]).only(
            "dedup_key", "likes", "retweets", "views"
        )
    }
    Tweet.objects.bulk_create(
        rows,
        update_conflicts=True,
        unique_fields=["dedup_key"],
        update_fields=_TWEET_UPDATE_FIELDS,
    )
    _record_metrics(rows, previous)
    return len(batch)


def _record_metrics(rows: list[Tweet], previous: dict[str, Tweet]) -> None:
    keys = [row.dedup_key for row in rows]
    saved = {
        t.dedup_key: t
        for t in Tweet.objects.filter(dedup_key__in=keys).only("id", "dedup_key")
    }
    snapshots = []
    for row in rows:
        old = previous.get(row.dedup_key)
        if old is not None and (old.likes, old.retweets, old.views) == (
            row.likes, row.retweets, row.views
        ):
            continue
        tweet = saved.get(row.dedup_key)
        if tweet is None:
            continue
        snapshots.append(
            TweetMetric(
                tweet=tweet,
                likes=row.likes or 0,
                retweets=row.retweets or 0,
                views=row.views or 0,
            )
        )
    if snapshots:
        TweetMetric.objects.bulk_create(snapshots)


def ingest_search_results(search: Search, items) -> int:
    """Upsert tweets and link them to a search, ranked by arrival order."""
    batch = list(items)
    if not batch:
        return 0
    ingest_tweets(batch)
    keys = []
    for item in batch:
        key = dedup_key(item)
        if key:
            keys.append(key)
    tweets = {t.dedup_key: t for t in Tweet.objects.filter(dedup_key__in=keys)}
    links = []
    for rank, item in enumerate(batch):
        tweet = tweets.get(dedup_key(item))
        if tweet is None:
            continue
        links.append(SearchResult(search=search, tweet=tweet, rank=rank))
    if links:
        SearchResult.objects.bulk_create(
            links,
            update_conflicts=True,
            unique_fields=["search", "tweet"],
            update_fields=["rank"],
        )
    return len(links)
