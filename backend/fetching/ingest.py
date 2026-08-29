"""Upsert normalized fetcher tweet dicts into Postgres."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

from tweets.models import Search, SearchHit, SearchTweet, Tweet, TweetMetric, TwitterUser
from tweets.textclean import clean_text

from . import metric_gates

logger = logging.getLogger(__name__)

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
    "text_clean",
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

# Deliberately NOT in _TWEET_UPDATE_FIELDS: source_subsystem records which
# pipeline *first* captured a tweet, and the live poll re-sees backfilled tweets
# constantly. Including it would hand every historical tweet to "live" within a
# cycle or two and make the collection-flow chart a lie.

# SearchTweet carries no source_subsystem (it is always "search") and no author
# FK churn beyond what _row_fields resolves, so its refresh set is the same list
# minus the column that does not exist on that table.
_SEARCH_TWEET_UPDATE_FIELDS = list(_TWEET_UPDATE_FIELDS)


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


METRIC_FIELDS = ("likes", "retweets", "replies", "quotes", "bookmarks", "views")


def metrics_for(item: dict) -> dict[str, int]:
    """Engagement to store on the row, resolved to the post readers actually see.

    A repost is not its own post: X returns the wrapper with the *original's*
    like and repost counts but no view count of its own, because nobody views
    the wrapper. Storing the wrapper verbatim gave 3,218 rows with thousands of
    reposts and zero views -- impossible on its face, and it silently corrupted
    every view-based ranking, since those tweets are exactly the widely-shared
    ones. The real number was already being captured one level down in
    extras.retweeted_tweet.metrics; it just was not the thing written to the
    columns analytics read.

    Per-field fallback rather than wholesale substitution: a wrapper that does
    carry a figure keeps it, so this can only ever fill a gap.
    """
    resolved = {field: _count(item.get(field)) for field in METRIC_FIELDS}
    if item.get("type") != "Retweet":
        return resolved
    original = item.get("retweeted_tweet") or {}
    source = original.get("metrics") if isinstance(original, dict) else None
    if not isinstance(source, dict):
        return resolved
    for field in METRIC_FIELDS:
        if not resolved[field]:
            resolved[field] = _count(source.get(field))
    return resolved


def _count(raw) -> int:
    """One engagement figure as an int, never raising.

    A bare int() would turn a single upstream format change ("1,200", "12K")
    into a ValueError inside _tweet_row, aborting the whole ingest batch so
    nothing from that fetch lands at all. Losing one number is a data-quality
    event that metric_gates is built to report; losing the batch is an outage.
    """
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


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


def account_of(item: dict) -> str:
    """The handle a normalized item belongs to, spelled the way rows store it."""
    author_data = item.get("author", {}) if isinstance(item.get("author"), dict) else {}
    return str(item.get("account") or author_data.get("handle") or "unknown").lstrip("@").lower()


def _row_fields(item: dict, authors_by_handle: dict[str, TwitterUser]) -> dict | None:
    """The normalized column values shared by Tweet and SearchTweet.

    One parser for both tables. The two models differ only in which columns they
    carry (SearchTweet has no source_subsystem -- for that table it is always
    "search"), never in how a field is derived from the engine's payload.
    """
    key = dedup_key(item)
    if not key:
        return None
    account = account_of(item)
    return {
        "dedup_key": key,
        "tweet_id": str(item.get("rest_id") or item.get("id") or item.get("tweet_id") or ""),
        "author_rest_id": item.get("author_id"),
        "account": account,
        "author": authors_by_handle.get(account),
        "text": item.get("text") or "",
        "text_clean": clean_text(item.get("text")),
        "url": item.get("url") or "",
        "type": item.get("type") or "Tweet",
        # Leave NULL when the timestamp is unparseable. Stamping now() would sort
        # the tweet to the top of the feed forever, and raw_created_at preserves
        # the original string for diagnosis.
        "created_at": _parse_created_at(item.get("created_at") or item.get("raw_timestamp")),
        "raw_created_at": str(item.get("created_at") or ""),
        **metrics_for(item),
        "source_language": item.get("source_language"),
        "source_endpoint": item.get("source_endpoint") or "",
        "conversation_id": item.get("conversation_id"),
        "entities": item.get("entities") or {},
        "extras": _extras_from_item(item),
        "payload": item,
    }


def _tweet_row(
    item: dict, authors_by_handle: dict[str, TwitterUser], subsystem: str = ""
) -> Tweet | None:
    fields = _row_fields(item, authors_by_handle)
    if fields is None:
        return None
    return Tweet(source_subsystem=subsystem, **fields)


def _search_tweet_row(
    item: dict, authors_by_handle: dict[str, TwitterUser]
) -> SearchTweet | None:
    fields = _row_fields(item, authors_by_handle)
    if fields is None:
        return None
    # SearchTimeline is the only endpoint that feeds this table; the engine does
    # not always stamp source_endpoint on a search item, so default it here
    # rather than storing a blank that reads as "unknown provenance".
    fields["source_endpoint"] = fields["source_endpoint"] or "SearchTimeline"
    return SearchTweet(**fields)


def _upsert_authors(items: Iterable[dict]) -> dict[str, TwitterUser]:
    pending: dict[str, dict] = {}
    for item in items:
        account = account_of(item)
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


def upsert_tweet(item: dict, subsystem: str = "") -> Tweet | None:
    authors = _upsert_authors([item])
    row = _tweet_row(item, authors, subsystem)
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


class IngestCount(int):
    """How many rows were upserted, carrying how many of them were new.

    An int subclass rather than a tuple or dataclass so every existing caller --
    `count += ingest(...)`, `finalize_run(ingested_tweets=count)`, the tests'
    `== 3` -- keeps working unchanged, while the one number they were all
    missing rides along.

    That number matters: a search run that re-sees the same 40 hits it stored an
    hour ago was reporting "40 results stored" every 30 minutes forever, which is
    precisely the "a run that did nothing must not look healthy" rule the project
    already applies to run status.
    """

    new: int = 0

    def __new__(cls, seen: int, new: int = 0):
        value = super().__new__(cls, seen)
        value.new = int(new)
        return value


def ingest_tweets(items, subsystem: str = "") -> IngestCount:
    batch = [item for item in items if isinstance(item, dict)]
    if not batch:
        return IngestCount(0, 0)
    authors = _upsert_authors(batch)
    rows: list[Tweet] = []
    seen: set[str] = set()
    for item in batch:
        row = _tweet_row(item, authors, subsystem)
        if row is None or row.dedup_key in seen:
            continue
        seen.add(row.dedup_key)
        rows.append(row)
    if not rows:
        return IngestCount(0, 0)
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
    logger.info(
        "ingested %d tweet(s): %d unique row(s) upserted, %d duplicate(s) collapsed",
        len(batch), len(rows), len(batch) - len(rows),
    )
    _log_metric_violations(rows)
    # `previous` was already loaded to diff metrics, so the new-row count is free.
    return IngestCount(len(batch), len(rows) - len(previous))


def _log_metric_violations(rows: list[Tweet]) -> None:
    """Surface impossible engagement counts at the moment they are written.

    A parse regression here is invisible in a green run -- the rows land, the
    counts are just wrong -- so it has to announce itself or it gets found
    months later in a chart nobody trusts.
    """
    violations = metric_gates.summarize(rows)
    if violations:
        # Offending rows, not violation count: one row can trip three
        # invariants at once, which is how "1500/1000 row(s) failed" happens.
        offenders = sum(1 for row in rows if metric_gates.check_metrics(
            created_at=row.created_at, likes=row.likes, retweets=row.retweets,
            replies=row.replies, views=row.views,
        ))
        logger.warning(
            "ingest: %d/%d row(s) failed an engagement sanity check: %s",
            offenders, len(rows),
            ", ".join(f"{name}={count}" for name, count in sorted(violations.items())),
        )


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


def ingest_search_hits(search: Search, items) -> IngestCount:
    """Upsert SearchTimeline hits and link them to a search, in arrival order.

    Writes to SearchTweet/SearchHit, never to Tweet. The two collectors are kept
    in separate tables: UserTweets owns the tracked-account archive that the feed
    and the engagement analytics read, and a saved search is a transient view of
    the wider firehose with its own 30-day retention. Mixing them made the feed a
    blend of both and gave search hits the archive's retention by accident.
    """
    batch = [item for item in items if isinstance(item, dict)]
    if not batch:
        return IngestCount(0, 0)
    authors = _upsert_authors(batch)
    rows: list[SearchTweet] = []
    ranks: dict[str, int] = {}
    for rank, item in enumerate(batch):
        row = _search_tweet_row(item, authors)
        # First occurrence wins the rank: a query that returns the same post
        # twice in one page should not push it down the result list.
        if row is None or row.dedup_key in ranks:
            continue
        ranks[row.dedup_key] = rank
        rows.append(row)
    if not rows:
        return IngestCount(0, 0)
    # Which of these this search had already linked, resolved before the upsert
    # so "new" means new *to this query*, not new to the SearchTweet table -- two
    # searches can legitimately match the same post.
    already_linked = set(
        SearchHit.objects.filter(
            search=search, search_tweet__dedup_key__in=list(ranks)
        ).values_list("search_tweet__dedup_key", flat=True)
    )
    SearchTweet.objects.bulk_create(
        rows,
        update_conflicts=True,
        unique_fields=["dedup_key"],
        update_fields=_SEARCH_TWEET_UPDATE_FIELDS,
    )
    saved = {
        row.dedup_key: row
        for row in SearchTweet.objects.filter(dedup_key__in=list(ranks)).only("id", "dedup_key")
    }
    links = [
        SearchHit(search=search, search_tweet=saved[key], rank=rank)
        for key, rank in ranks.items()
        if key in saved
    ]
    if links:
        SearchHit.objects.bulk_create(
            links,
            update_conflicts=True,
            unique_fields=["search", "search_tweet"],
            # last_seen_at is auto_now, which bulk_create's upsert path does not
            # refresh, so it is listed explicitly -- it is how the console shows
            # that a repoll re-saw a result rather than only first finding it.
            update_fields=["rank", "last_seen_at"],
        )
    logger.info(
        "search %r: linked %d/%d fetched result(s)", search.name, len(links), len(batch),
    )
    return IngestCount(len(links), sum(1 for key in ranks if key not in already_linked))
