"""How a feed request becomes a queryset.

This is the tracked-account stream's query policy: which accounts, which post
types, which window, which sort. It lives apart from tweets/views.py because it
is not a transport concern and because it has a second consumer -- the CSV
exporter on the control worker (fetching/exports.py) runs the identical query
for a saved set of query parameters. That module used to reach into the view
module to get it, which is a data-access layer importing a transport one.

Takes a QueryDict (or any mapping); returns an unevaluated queryset. It raises
DRF's ValidationError for an unparseable timestamp, which is the one piece of
HTTP vocabulary here and is deliberate: the export worker re-validates a stored
query string through the same path, so a typo is rejected identically whether it
arrives over HTTP or off a job row.
"""
from __future__ import annotations

from datetime import timedelta, timezone as dt_timezone

from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from engine.processing import TZ as FEED_TZ

from .models import FetchRun, Tweet, TwitterUser
from .windows import normalize_handles, parse_instant


def with_feed_ts(qs):
    """Annotate the cursor-ordering field used by StandardCursorPagination.

    `created_at` is when X says the tweet was posted and is nullable when the
    timestamp will not parse; `ingested_at` is when we saw it and is never null.
    Coalescing gives a non-null, monotonic ordering key, so an undated tweet
    sorts by when it arrived instead of being fabricated a "now" timestamp (which
    pinned it to the top of the feed forever) or dropped from the feed entirely.
    """
    return qs.annotate(feed_ts=Coalesce("created_at", "ingested_at"))


# Tweet.type as the engine writes it (engine/processing.py), keyed by the
# lowercase name the console uses in its filter chips.
POST_TYPES = {"tweet": "Tweet", "reply": "Reply", "retweet": "Retweet", "quote": "Quote"}

# Sugar over since/until so the console can offer one-click windows.
FEED_WINDOWS = {"1h": 1, "6h": 6, "24h": 24, "7d": 168, "30d": 720}

# Calendar windows, resolved against the Tehran day the collector already counts
# in (engine/processing.TZ). "Today" used to be an alias for a rolling 24h,
# which meant that at 00:30 Tehran the feed labelled "Today" was almost entirely
# yesterday. These snap to real boundaries so the label is the truth.
FEED_CALENDAR_WINDOWS = ("today", "week", "month")


def feed_instant(params, name: str):
    """One `?since=`/`?until=` value as an aware datetime, or None if absent.

    The raw string used to go straight into `created_at__gte`, where Django's
    field coercion raises `django.core.exceptions.ValidationError` -- which DRF
    does not handle, so `?since=garbage` was a 500 rather than a 400. The same
    string is stored verbatim on an export job, so a typo there failed the job
    on the worker with an opaque message instead of being rejected on the way in.
    """
    raw = params.get(name)
    if not raw:
        return None
    parsed = parse_instant(raw)
    if parsed is None:
        raise ValidationError(
            {name: f"Expected an ISO 8601 timestamp, got {str(raw)[:100]!r}."}
        )
    return parsed


def _calendar_start(window: str):
    """Start of the current Tehran day/week/month, as a UTC-aware datetime."""
    local = timezone.now().astimezone(FEED_TZ)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "today":
        start = midnight
    elif window == "week":
        # Tehran / Iranian calendar week begins on Saturday (Shanbeh).
        # Python weekday(): Mon=0..Sat=5, Sun=6.
        # (weekday + 2) % 7 gives days since Saturday (Sat=0, Sun=1, ..., Fri=6).
        days_since_saturday = (midnight.weekday() + 2) % 7
        start = midnight - timedelta(days=days_since_saturday)
    else:
        start = midnight.replace(day=1)
    return start.astimezone(dt_timezone.utc)


def feed_queryset(params):
    """The tracked-account stream: everything the UserTweets collector captured.

    Saved searches are deliberately absent. They used to be unioned in here, so
    the feed was a blend of two collectors with different cadences, different
    retention and different meanings of "why is this post here" -- and a query
    the operator had merely saved silently rewrote everyone's feed. Search hits
    now live in their own tables and are read through /api/searches/{id}/results/.
    """
    tracked = list(
        TwitterUser.objects.filter(tracking=True).values_list("handle", "priority")
    )
    handle_to_priority = dict(tracked)
    # Posts whose account has since been untracked are still archived (Tweet has
    # no TTL) but used to be unreachable from every screen while still being
    # counted in "archive total". Default stays tracked-only so the feed keeps
    # meaning "the timelines you follow"; ?include_untracked=1 opens the rest.
    if str(params.get("include_untracked") or "") in {"1", "true", "yes"}:
        qs = Tweet.objects.all()
    else:
        qs = Tweet.objects.filter(account__in=list(handle_to_priority))
    # Repeatable ?account=, matching the analytics endpoints. params.get() would
    # silently keep only the last value, so a two-account selection returned one
    # account's posts.
    accounts = normalize_handles(
        params.getlist("account") if hasattr(params, "getlist") else [params.get("account")]
    )
    if accounts:
        qs = qs.filter(account__in=accounts)
    requested = [name.strip() for name in str(params.get("types") or "").lower().split(",")]
    types = [POST_TYPES[name] for name in requested if name in POST_TYPES]
    if types:
        qs = qs.filter(type__in=types)
    if str(params.get("has_media") or "") in {"1", "true", "yes"}:
        # extras["media"] is the normalized media list; a tweet without media
        # stores [] there, and rows predating extras store null.
        qs = qs.exclude(extras__media=[]).exclude(extras__media=None)
    window = str(params.get("window") or "").lower()
    if window in FEED_WINDOWS:
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(hours=FEED_WINDOWS[window]))
    elif window in FEED_CALENDAR_WINDOWS:
        qs = qs.filter(created_at__gte=_calendar_start(window))
    tier = params.get("tier")
    if tier:
        try:
            priority = int(tier)
            tier_handles = [
                handle for handle, value in handle_to_priority.items() if value == priority
            ]
            qs = qs.filter(account__in=tier_handles)
        except ValueError:
            pass
    since = feed_instant(params, "since")
    if since:
        qs = qs.filter(created_at__gte=since)
    until = feed_instant(params, "until")
    if until:
        qs = qs.filter(created_at__lte=until)
    run_id = params.get("run_id")
    if run_id:
        run = FetchRun.objects.filter(run_id=run_id).first()
        if run is not None:
            qs = qs.filter(ingested_at__gte=run.started_at)
            if run.finished_at:
                qs = qs.filter(ingested_at__lte=run.finished_at)
    query = (params.get("q") or "").strip()
    if query:
        # Substring, not full-text. `to_tsvector` matching meant "the", "and" and
        # "a" returned an empty archive (they are English stop words) while
        # "bitco" could never find "Bitcoin", because a lexeme is not a prefix.
        # A search box should behave like Ctrl+F. Matching on text_clean rather
        # than text means a search for "R&D" is not defeated by the stored
        # "R&amp;D"; the trgm index from migration 0017 keeps it fast, and
        # icontains spells the same on SQLite so tests exercise real semantics.
        qs = qs.filter(text_clean__icontains=query)
    # No .distinct() any more: the union with search results was the only thing
    # that could produce a duplicate row, and distinct() over a deferred JSONB
    # payload is expensive on a table this size.
    qs = with_feed_ts(qs.select_related("author").defer("payload"))
    sort = str(params.get("sort") or "").lower()
    # Both ranked sorts order on a stored, indexed column and break ties on id.
    #
    # `top` used to annotate the engagement expression and sort on that, which
    # no index can serve -- over the All-time window the console offers, that
    # was a full scan and sort of the archive on every request. Tweet.engagement
    # is now a persisted generated column with its own index.
    #
    # The tiebreaker is id, not feed_ts: the index is (-engagement, -id), and a
    # middle key the index does not carry forces a sort of every tied group.
    # Ties here are tweets with identical scores, where recency-vs-id is not a
    # meaningful distinction anyway.
    if sort == "top":
        return qs.order_by("-engagement", "-id")
    if sort == "views":
        # Reach, asked as its own question rather than folded into engagement.
        return qs.order_by("-views", "-id")
    return qs.order_by("-feed_ts", "-id")
