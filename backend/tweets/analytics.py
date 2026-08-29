"""Read-only archive analytics backed by the existing Postgres tables.

Everything here shares one window contract:

    ?range=24h|7d|30d|90d      (or ?since=&until= ISO timestamps)
    ?bucket=auto|hour|day|week (auto: hourly under 48h, daily above)
    ?account=a&account=b       (repeatable; omitted means every account)

so the console can drive Pulse, Feed and Analyze from a single filter bar.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.db import connection, transaction, OperationalError
from django.db.models import Avg, Count, ExpressionWrapper, F, FloatField, Q, Sum
from django.db.models.functions import Trunc
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.response import Response
from rest_framework.views import APIView

from fetching.accounts import archive_progress
# parse_since already turns "24h"/"30d" into a timedelta for `manage.py
# fetch_report`; one spelling of the range syntax for the CLI and the API both.
from fetching.management.commands.fetch_report import parse_since

from tweets import topics
from tweets.models import FetchRun, KeyValueState, Search, SearchTweet, Tweet, TwitterUser
from tweets.permissions import IsStaff
from tweets.serializers import TweetSerializer, _new_tweets

# Single source of truth for "engagement": kept as field names so both the ORM
# expression below and the raw-SQL views can build the same formula from it.
#
# Views are deliberately NOT in here. An impression is not an interaction, and
# views run 100-1000x larger than every other metric, so summing them in makes
# "most engaged" a synonym for "most viewed" and buries a 48k-like post under a
# 47k-like one that happened to be shown more. Reach is its own question and has
# its own sort (`sort=views`); this is the deliberate-action number.
ENGAGEMENT_FIELDS = ("likes", "retweets", "replies", "quotes")
# tweets_tweetmetric only snapshots likes/retweets/views, so velocity -- which
# differences consecutive snapshots -- can only speak for the fields it stores.
METRIC_ENGAGEMENT_FIELDS = ("likes", "retweets")


def _engagement_sql(prefix: str = "", fields: tuple[str, ...] = ENGAGEMENT_FIELDS) -> str:
    """The engagement formula as SQL, table-qualified when a query joins.

    tweets_tweet and tweets_tweetmetric both carry likes/retweets/views, so an
    unqualified sum is ambiguous the moment the two are joined.
    """
    return " + ".join(f"{prefix}{field}" for field in fields)

# The archive can outlive any window we chart, but a 90-day hourly scan is the
# point where these queries stop being interactive. Cap rather than let a
# hand-edited URL table-scan the whole tweet table.
MAX_WINDOW_HOURS = 24 * 90
DEFAULT_RANGE = "24h"
# date_trunc/Trunc kinds we allow. Never interpolate a raw request value into
# SQL -- membership in this dict is what makes the raw-SQL views injection-safe.
BUCKET_KINDS = {"hour": "hour", "day": "day", "week": "week"}
AUTO_BUCKET_HOUR_LIMIT = 48

SUBSYSTEMS = ("live", "historical", "search")


def engagement_expression() -> ExpressionWrapper:
    total = sum(F(field) for field in ENGAGEMENT_FIELDS)
    return ExpressionWrapper(total, output_field=FloatField())


@dataclass(frozen=True)
class Window:
    since: datetime
    until: datetime
    bucket: str

    @property
    def hours(self) -> float:
        return (self.until - self.since).total_seconds() / 3600

    @property
    def previous_since(self) -> datetime:
        """Start of the equal-length period immediately before this one."""
        return self.since - (self.until - self.since)


def _parse_when(value) -> datetime | None:
    parsed = parse_datetime(str(value).replace("Z", "+00:00")) if value else None
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def window_from(request) -> Window:
    """Resolve range/since/until/bucket into one clamped, validated window."""
    until = _parse_when(request.query_params.get("until")) or timezone.now()
    since = _parse_when(request.query_params.get("since"))
    if since is None:
        try:
            span = parse_since(request.query_params.get("range") or DEFAULT_RANGE)
        except ValueError:
            span = parse_since(DEFAULT_RANGE)
        since = until - span
    if since >= until:
        since = until - parse_since(DEFAULT_RANGE)
    since = max(since, until - timedelta(hours=MAX_WINDOW_HOURS))

    bucket = str(request.query_params.get("bucket") or "auto").lower()
    if bucket not in BUCKET_KINDS:
        span_hours = (until - since).total_seconds() / 3600
        bucket = "hour" if span_hours <= AUTO_BUCKET_HOUR_LIMIT else "day"
    return Window(since=since, until=until, bucket=bucket)


def normalize_handles(values) -> list[str]:
    """Normalize a list of ?account= values the way handles are stored.

    Also accepts one comma-joined value, which is what a URL built from a
    multi-select naturally produces. Shared with the feed so both surfaces spell
    the account filter the same way.
    """
    handles = [part for value in values or [] for part in str(value).split(",")]
    return sorted({h.strip().lstrip("@").lower() for h in handles if h.strip()})


def accounts_from(request) -> list[str]:
    """Repeatable ?account= filter, normalized the way handles are stored."""
    return normalize_handles(request.query_params.getlist("account"))


def _in_window(qs, window: Window, field: str = "created_at"):
    return qs.filter(**{f"{field}__gte": window.since, f"{field}__lte": window.until})


def _for_accounts(qs, handles: list[str]):
    return qs.filter(account__in=handles) if handles else qs


def _series(
    qs,
    window: Window,
    field: str,
    *,
    group: str | None = None,
    constant_group: tuple[str, str] | None = None,
) -> list[dict]:
    """Bucketed counts, optionally split by a second column.

    ORM rather than raw SQL so this works on the SQLite test database too --
    only the JSON/trigram views below genuinely need Postgres.

    `constant_group` labels every row with a fixed (key, value) instead of
    grouping. SearchTweet has no source_subsystem column -- for that table it is
    always "search" -- and this lets its series concatenate with the Tweet one
    into a single long-format list the console pivots without special cases.
    """
    if constant_group is not None:
        key, value = constant_group
        return [
            {**row, key: value} for row in _series(qs, window, field)
        ]
    values = ["bucket"] + ([group] if group else [])
    rows = (
        _in_window(qs, window, field)
        # order_by() before the grouping: both Tweet and FetchRun declare a
        # Meta.ordering, and Django folds a model's default ordering into the
        # GROUP BY, which silently un-groups the aggregate.
        .order_by()
        .annotate(bucket=Trunc(field, BUCKET_KINDS[window.bucket]))
        .values(*values)
        .annotate(count=Count("id"))
        .order_by("bucket")
    )
    return [
        {
            "bucket": row["bucket"].isoformat() if row["bucket"] else None,
            "count": int(row["count"]),
            **({group: row[group] or "unknown"} if group else {}),
        }
        for row in rows
    ]


def _float_param(request, param: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(request.query_params.get(param, default))))
    except (TypeError, ValueError):
        return default


def _int_param(request, param: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(request.query_params.get(param, default))))
    except (TypeError, ValueError):
        return default


class OverviewView(APIView):
    def get(self, request):
        window = window_from(request)
        latest = list(FetchRun.objects.all()[:5].values(
            "subsystem", "status", "started_at", "finished_at", "summary"
        ))
        tweet_counts = Tweet.objects.aggregate(
            total=Count("id"),
            in_window=Count("id", filter=Q(created_at__gte=window.since)),
        )
        account_counts = TwitterUser.objects.aggregate(
            tracked=Count("id", filter=Q(tracking=True)),
            quarantined=Count("id", filter=Q(quarantined=True)),
        )
        return Response({
            "tweets": tweet_counts["total"],
            "tweets_in_window": tweet_counts["in_window"],
            "tracked_accounts": account_counts["tracked"],
            "quarantined_accounts": account_counts["quarantined"],
            "latest_runs": latest,
        })


# --- Ingestion: how the collector is spending its budget over time ----------


def _request_spend(window: Window) -> list[dict]:
    """Requests per bucket per endpoint, summed out of FetchRun.summary.

    Postgres-only: the counts live in a JSON object keyed by endpoint, and the
    alternative is dragging every run row (each carrying up to 100 recent
    events) into Python. Empty on SQLite, like the other JSON/trigram views.
    """
    if connection.vendor != "postgresql":
        return []
    kind = BUCKET_KINDS[window.bucket]
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT date_trunc('{kind}', r.started_at) AS bucket,
                   spend.key AS endpoint,
                   sum(spend.value::int) AS pages
            FROM tweets_fetchrun r
            CROSS JOIN LATERAL jsonb_each_text(
                COALESCE(r.summary->'pages_by_endpoint', '{{}}'::jsonb)
            ) AS spend
            WHERE r.started_at >= %s AND r.started_at <= %s
            GROUP BY 1, 2
            ORDER BY 1, 2
            """,
            [window.since, window.until],
        )
        pages = cursor.fetchall()
        cursor.execute(
            f"""
            SELECT date_trunc('{kind}', r.started_at) AS bucket,
                   errors.key AS status_code,
                   sum(errors.value::int) AS requests
            FROM tweets_fetchrun r
            CROSS JOIN LATERAL jsonb_each_text(
                COALESCE(r.summary->'http_errors_by_status', '{{}}'::jsonb)
            ) AS errors
            WHERE r.started_at >= %s AND r.started_at <= %s
            GROUP BY 1, 2
            ORDER BY 1, 2
            """,
            [window.since, window.until],
        )
        errors = cursor.fetchall()
    return [
        {"bucket": bucket.isoformat(), "endpoint": endpoint, "requests": int(count), "kind": "ok"}
        for bucket, endpoint, count in pages
    ] + [
        {"bucket": bucket.isoformat(), "endpoint": f"HTTP {code}", "requests": int(count), "kind": "error"}
        for bucket, code, count in errors
    ]


class IngestionView(APIView):
    """The Pulse time series: what arrived, from where, and what it cost."""

    def get(self, request):
        window = window_from(request)
        handles = accounts_from(request)
        tweets = _for_accounts(Tweet.objects.all(), handles)
        # Search hits live in their own table now, so the collection-flow chart
        # has to union them back in or the "search" series silently reads zero
        # and the console claims a whole collector stopped working.
        hits = _for_accounts(SearchTweet.objects.all(), handles)

        previous = Window(since=window.previous_since, until=window.since, bucket=window.bucket)
        # Kept apart as well as summed. These are two different stores with two
        # different retentions -- archive tweets are permanent, search hits expire
        # after 30 days -- so a single "captured" number could read 60K over 90
        # days against an archive total of 53K, which looks impossible.
        captured_archive = _in_window(tweets, window, "ingested_at").count()
        captured_search = _in_window(hits, window, "ingested_at").count()
        captured = captured_archive + captured_search
        captured_before = (
            _in_window(tweets, previous, "ingested_at").count()
            + _in_window(hits, previous, "ingested_at").count()
        )
        # Whether there is a previous period to compare against at all. Without
        # this the 90d view reported "+60K vs the previous equal period" when the
        # deployment was 13 days old -- the delta was the whole value, dressed up
        # as growth.
        first_ingest = (
            Tweet.objects.order_by("ingested_at")
            .values_list("ingested_at", flat=True)
            .first()
        )
        has_previous = bool(first_ingest and first_ingest <= previous.since)

        runs = FetchRun.objects.filter(
            started_at__gte=window.since, started_at__lte=window.until
        )
        run_totals = runs.order_by().values("subsystem", "status").annotate(count=Count("id"))

        by_subsystem = {
            row["source_subsystem"] or "unknown": row["count"]
            for row in _in_window(tweets, window, "ingested_at")
            .order_by()
            .values("source_subsystem")
            .annotate(count=Count("id"))
        }
        search_captured = _in_window(hits, window, "ingested_at").count()
        if search_captured:
            by_subsystem["search"] = by_subsystem.get("search", 0) + search_captured

        return Response({
            "since": window.since.isoformat(),
            "until": window.until.isoformat(),
            "bucket": window.bucket,
            # Who is doing the collecting, bucketed over the window.
            "captured": _series(tweets, window, "ingested_at", group="source_subsystem")
            + _series(hits, window, "ingested_at", constant_group=("source_subsystem", "search")),
            # Which slice of history the archive now covers, by posted date.
            "posted": _series(tweets, window, "created_at"),
            "runs": _series(runs, window, "started_at", group="subsystem"),
            "run_totals": [
                {"subsystem": row["subsystem"], "status": row["status"], "count": row["count"]}
                for row in run_totals
            ],
            "requests": _request_spend(window),
            "totals": {
                "captured": captured,
                "captured_archive": captured_archive,
                "captured_search": captured_search,
                "captured_previous": captured_before,
                "captured_delta": captured - captured_before,
                # False means "we did not exist for all of the previous period",
                # and the console must not render a delta from it.
                "has_previous": has_previous,
                "by_subsystem": by_subsystem,
                # The tracked-account archive only. Search hits are a rolling
                # 30-day view of the firehose, not part of what we have archived.
                "archive_total": Tweet.objects.count(),
                # Of that total, how much the feed can actually reach. Posts whose
                # account was later untracked stay archived but fall outside the
                # feed's tracked-account filter, so a single headline number
                # advertised 6K posts no screen could open.
                "archive_tracked": Tweet.objects.filter(
                    account__in=TwitterUser.objects.filter(tracking=True).values("handle")
                ).count(),
                "search_total": SearchTweet.objects.count(),
                "oldest_tweet": (
                    Tweet.objects.filter(created_at__isnull=False)
                    .order_by("created_at")
                    .values_list("created_at", flat=True)
                    .first()
                ),
            },
        })


# Endpoints the collectors actually call. `UserTweetsAndReplies` still exists in
# the transport layer (query ids, browser allow-list, status handling) but no
# pipeline requests it -- `4_union` from UserTweets is the only processed output.
# Reporting its untouched 500/500 budget and a permanent "HEALTHY" told the
# operator a collector was working that has never run.
REPORTED_ENDPOINTS = ("UserTweets", "SearchTimeline", "UserByScreenName", "TweetDetail")


def _rate_limits() -> list[dict]:
    """Live X quota per endpoint, as the engine last persisted it.

    The engine writes rate_limits.json into its scratch state dir; the runner
    round-trips that into KeyValueState under one row per subsystem group.
    """
    now = timezone.now().timestamp()
    limits: dict[str, dict] = {}
    rows = KeyValueState.objects.filter(
        namespace="request_state", name__endswith="rate_limits.json"
    )
    for row in rows:
        data = row.data if isinstance(row.data, dict) else {}
        for endpoint, state in data.items():
            if not isinstance(state, dict) or endpoint not in REPORTED_ENDPOINTS:
                continue
            reset = int(state.get("reset") or 0)
            current = limits.get(endpoint)
            # Two state rows (historical_live: and search:) can both carry an
            # endpoint; the fresher reset is the one that reflects reality.
            if current is not None and current["reset_epoch"] >= reset:
                continue
            limits[endpoint] = {
                "endpoint": endpoint,
                "remaining": int(state.get("remaining") or 0),
                "limit": int(state.get("limit") or 0),
                "reset_epoch": reset,
                "resets_in_seconds": max(0, int(reset - now)),
            }
    return sorted(limits.values(), key=lambda row: row["endpoint"])


def _endpoint_health() -> dict[str, str]:
    health: dict[str, str] = {}
    for row in KeyValueState.objects.filter(
        namespace="request_state", name__endswith="endpoint_health.json"
    ):
        if isinstance(row.data, dict):
            health.update({
                str(k): str(v)
                for k, v in row.data.items()
                if k in REPORTED_ENDPOINTS
            })
    return health


_SUBSYSTEM_INTERVALS = {
    "live": "FETCH_LIVE_INTERVAL_SECONDS",
    "historical": "FETCH_HISTORICAL_INTERVAL_SECONDS",
    "search": "FETCH_SEARCH_DISPATCH_SECONDS",
}


class PipelineView(APIView):
    """Point-in-time collector state: quota, cadence, backfill, what's running."""

    def get(self, request):
        now = timezone.now()
        subsystems = []
        for name in SUBSYSTEMS:
            last = FetchRun.objects.filter(subsystem=name).exclude(status="running").first()
            interval = int(getattr(settings, _SUBSYSTEM_INTERVALS[name]))
            due_at = last.started_at + timedelta(seconds=interval) if last else None
            subsystems.append({
                "subsystem": name,
                "interval_seconds": interval,
                "running": FetchRun.objects.filter(subsystem=name, status="running").count(),
                "last_run": {
                    "run_id": last.run_id,
                    "status": last.status,
                    "target": last.target,
                    "started_at": last.started_at,
                    "finished_at": last.finished_at,
                    "ingested_tweets": int((last.summary or {}).get("ingested_tweets") or 0),
                    # The dashboard's "+N posts" line. It said "+51 posts" for an
                    # archive walk that had added nothing, directly contradicting
                    # the collection-flow chart beside it.
                    "new_tweets": _new_tweets(last),
                } if last else None,
                "next_due_in_seconds": (
                    max(0, int((due_at - now).total_seconds())) if due_at else 0
                ),
            })

        progress = archive_progress()
        stalled = [row for row in progress["walking"] if row["stalled_ticks"] > 0]
        return Response({
            "now": now.isoformat(),
            "subsystems": subsystems,
            "rate_limits": _rate_limits(),
            "endpoint_health": _endpoint_health(),
            "running": list(
                FetchRun.objects.filter(status="running").values(
                    "run_id", "subsystem", "target", "started_at"
                )
            ),
            "archive": {
                "complete": len(progress["complete"]),
                # Done being walked, but only because X refused to page deeper.
                # Kept out of `complete` so the console cannot claim an archive
                # is whole when it stops three months back.
                "depth_limited": len(progress["depth_limited"]),
                "tracked": progress["tracked"],
                "stalled": len(stalled),
                "walking": progress["walking"][:12],
            },
            "quarantined": list(
                TwitterUser.objects.filter(quarantined=True).values(
                    "handle", "quarantine_reason", "quarantined_at"
                )
            ),
            "searches": list(
                Search.objects.filter(enabled=True)
                .order_by("slug")
                .values("id", "slug", "product", "interval_seconds", "last_run_at")
            ),
        })


# --- Velocity ---------------------------------------------------------------


class VelocityView(APIView):
    """Rank tweets by engagement gained during the window, plus its shape over time."""

    def get(self, request):
        window = window_from(request)
        handles = accounts_from(request)
        if connection.vendor != "postgresql":
            return Response({"results": [], "series": []})
        kind = BUCKET_KINDS[window.bucket]
        account_filter = "AND t.account = ANY(%s)" if handles else ""
        engagement = _engagement_sql("m.", METRIC_ENGAGEMENT_FIELDS)
        params = [window.since, window.until]
        if handles:
            params.append(handles)
        with connection.cursor() as cursor:
            # Per tweet: engagement at the end of the window minus engagement at
            # the start, from the metric snapshots ingest writes on change.
            cursor.execute(
                f"""
                WITH points AS (
                    SELECT m.tweet_id, m.captured_at, {engagement} AS total,
                           row_number() OVER (PARTITION BY m.tweet_id ORDER BY m.captured_at) AS first_n,
                           row_number() OVER (PARTITION BY m.tweet_id ORDER BY m.captured_at DESC) AS last_n
                    FROM tweets_tweetmetric m
                    JOIN tweets_tweet t ON t.id = m.tweet_id
                    WHERE m.captured_at >= %s AND m.captured_at <= %s
                    {account_filter}
                ), deltas AS (
                    SELECT tweet_id,
                           max(total) FILTER (WHERE last_n = 1)
                         - max(total) FILTER (WHERE first_n = 1) AS velocity
                    FROM points
                    GROUP BY tweet_id
                    HAVING count(*) >= 2
                )
                SELECT tweet_id, velocity
                FROM deltas
                WHERE velocity > 0
                ORDER BY velocity DESC, tweet_id DESC
                LIMIT 50
                """,
                params,
            )
            rows = cursor.fetchall()
            # Engagement gained per bucket across the whole window: consecutive
            # snapshots of the same tweet differenced, then summed per bucket.
            cursor.execute(
                f"""
                WITH points AS (
                    SELECT m.tweet_id, m.captured_at, {engagement} AS total,
                           lag({engagement}) OVER (
                               PARTITION BY m.tweet_id ORDER BY m.captured_at
                           ) AS previous
                    FROM tweets_tweetmetric m
                    JOIN tweets_tweet t ON t.id = m.tweet_id
                    WHERE m.captured_at >= %s AND m.captured_at <= %s
                    {account_filter}
                )
                SELECT date_trunc('{kind}', captured_at) AS bucket,
                       sum(GREATEST(total - previous, 0)) AS gained,
                       count(DISTINCT tweet_id) AS tweets
                FROM points
                WHERE previous IS NOT NULL
                GROUP BY 1
                ORDER BY 1
                """,
                params,
            )
            series = cursor.fetchall()

        ids = [row[0] for row in rows]
        rates = {row[0]: int(row[1] or 0) for row in rows}
        tweets = {tweet.id: tweet for tweet in Tweet.objects.filter(id__in=ids).select_related("author")}
        data = []
        for tweet_id in ids:
            if tweet := tweets.get(tweet_id):
                row = TweetSerializer(tweet).data
                row["velocity"] = rates[tweet_id]
                data.append(row)
        return Response({
            "results": data,
            "series": [
                {"bucket": bucket.isoformat(), "gained": int(gained or 0), "tweets": int(count)}
                for bucket, gained, count in series
            ],
            "bucket": window.bucket,
        })


# --- Topics -----------------------------------------------------------------

# Stopwords for the phrase miner. to_tsvector('simple') is used rather than
# 'english' on purpose: the archive is multilingual, and English stemming
# mangles non-English text into nonsense tokens. 'simple' does no stemming and
# strips no stopwords, so the list has to live here.
_STOPWORDS = (
    "the a an and or but if then than that this these those of to in on at by for with "
    "from as is are was were be been being do does did have has had it its it's i you he "
    "she we they me him her them my your his our their not no so just about into over "
    "after before more most very can will would should could there here what when where "
    "who whom which how why all any both each few other some such only own same too s t "
    "don now am pre re rt via amp http https www com co"
).split()

_MIN_TOKEN_LENGTH = 3
_TOPIC_LIMIT = 50


# How many candidates the grouping SQL hands to the scorer. Larger than the
# number rendered on purpose: the support, filler and nested-gram filters all
# reject rows, so ranking a list already truncated to 50 by raw count would leave
# the panel half-empty exactly when the corpus is noisiest.
_CANDIDATE_LIMIT = 600


def _document_totals(window: Window, handles: list[str]) -> tuple[int, int]:
    """Posts in the current and previous window -- the denominators for a rate.

    Same scope as the miners below (retweets excluded), because a share computed
    against a different population than the numerator is not a share.
    """
    scoped = _for_accounts(Tweet.objects.exclude(type="Retweet"), handles)
    previous = Window(since=window.previous_since, until=window.since, bucket=window.bucket)
    return (
        _in_window(scoped, window).count(),
        _in_window(scoped, previous).count(),
    )


def _hashtag_topics(window: Window, handles: list[str]) -> list[topics.TermStats]:
    account_filter = "AND account = ANY(%s)" if handles else ""
    params = [window.previous_since, window.until]
    if handles:
        params.append(handles)
    params += [window.since, window.since, window.since, window.since]
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            WITH tags AS (
                SELECT lower(tag) AS topic, id, account, created_at
                FROM tweets_tweet
                CROSS JOIN LATERAL jsonb_array_elements_text(
                    COALESCE(entities->'hashtags', '[]'::jsonb)
                ) AS tag
                WHERE created_at >= %s AND created_at <= %s
                  -- A repost carries the original's hashtags verbatim. Counting
                  -- it again makes one viral post look like a movement.
                  AND type <> 'Retweet'
                {account_filter}
            )
            SELECT topic,
                   count(DISTINCT id) FILTER (WHERE created_at >= %s) AS docs,
                   count(DISTINCT account) FILTER (WHERE created_at >= %s) AS authors,
                   count(DISTINCT id) FILTER (WHERE created_at < %s) AS previous_docs
            FROM tags
            GROUP BY topic
            HAVING count(DISTINCT id) FILTER (WHERE created_at >= %s) > 0
            ORDER BY docs DESC, topic
            LIMIT {_CANDIDATE_LIMIT}
            """,
            params,
        )
        return [
            topics.TermStats(topic, "hashtag", int(docs), int(authors), int(previous))
            for topic, docs, authors, previous in cursor.fetchall()
        ]


def _phrase_topics(window: Window, handles: list[str]) -> list[topics.TermStats]:
    """Words and two-word phrases mined from tweet text, counted by document.

    Tokens come from splitting on non-alphanumerics rather than from
    to_tsvector: a tsvector is stored sorted by lexeme, so WITH ORDINALITY over
    one gives alphabetical neighbours and the bigram join would pair words that
    never appeared together. regexp_split_to_array preserves document order,
    and [:alnum:] is unicode-aware on a UTF-8 database, so non-Latin scripts
    survive the split intact.

    URLs, @mentions and #hashtags are stripped first -- hashtags are counted by
    their own dimension, and a URL is one enormous meaningless token.

    Counts are per *document*, not per occurrence. A post that says "gold" five
    times is one post talking about gold, and the occurrence count was how a
    single ranting thread used to reach the top of the chart.
    """
    account_filter = "AND account = ANY(%s)" if handles else ""
    params = [window.previous_since, window.until]
    if handles:
        params.append(handles)
    params += [_STOPWORDS, _MIN_TOKEN_LENGTH, window.since, window.since, window.since, window.since]
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            WITH scoped AS (
                -- text_clean, so the tokenizer is not mining "amp" out of every
                -- "&amp;" X escaped. Falls back to text for rows the backfill
                -- has not reached.
                SELECT id, account, created_at,
                       COALESCE(NULLIF(text_clean, ''), text) AS text
                FROM tweets_tweet
                WHERE created_at >= %s AND created_at <= %s
                  -- A repost is the original's text verbatim. Leaving them in
                  -- let one widely-shared post contribute hundreds of identical
                  -- documents, which is most of what made this panel unusable.
                  AND type <> 'Retweet'
                {account_filter}
            ), tokens AS (
                SELECT s.id, s.account, s.created_at, token.lexeme, token.position
                FROM scoped s
                CROSS JOIN LATERAL unnest(
                    regexp_split_to_array(
                        regexp_replace(
                            lower(s.text),
                            '(https?://\\S+|[@#][[:alnum:]_]+)', ' ', 'g'
                        ),
                        '[^[:alnum:]_]+'
                    )
                ) WITH ORDINALITY AS token(lexeme, position)
                WHERE NOT (token.lexeme = ANY(%s))
                  AND length(token.lexeme) >= %s
                  AND token.lexeme ~ '[^0-9_]'
            ), phrases AS (
                SELECT lexeme AS topic, id, account, created_at FROM tokens
                UNION ALL
                SELECT a.lexeme || ' ' || b.lexeme AS topic, a.id, a.account, a.created_at
                FROM tokens a
                JOIN tokens b ON b.id = a.id AND b.position = a.position + 1
            )
            SELECT topic,
                   count(DISTINCT id) FILTER (WHERE created_at >= %s) AS docs,
                   count(DISTINCT account) FILTER (WHERE created_at >= %s) AS authors,
                   count(DISTINCT id) FILTER (WHERE created_at < %s) AS previous_docs
            FROM phrases
            GROUP BY topic
            HAVING count(DISTINCT id) FILTER (WHERE created_at >= %s) > 1
            ORDER BY docs DESC, topic
            LIMIT {_CANDIDATE_LIMIT}
            """,
            params,
        )
        return [
            topics.TermStats(topic, "phrase", int(docs), int(authors), int(previous))
            for topic, docs, authors, previous in cursor.fetchall()
        ]


_BLOCKLIST_STATE = ("analytics", "topic_blocklist")


def topic_blocklist() -> set[str]:
    """Terms an operator has explicitly hidden.

    Stored in KeyValueState rather than its own table: it is one small list, and
    the generic namespaced-JSON store already exists for exactly this.
    """
    namespace, name = _BLOCKLIST_STATE
    row = KeyValueState.objects.filter(namespace=namespace, name=name).first()
    terms = (row.data or {}).get("terms") if row else None
    return {str(term).lower() for term in terms or []}


class TopicsView(APIView):
    """What is being discussed, ranked by what changed rather than what is common.

    `?rank=surging` (default) orders by how unusual a term's current rate is
    against the previous window of equal length; `?rank=volume` orders by how
    many posts mention it. The support, filler and nested-gram filters apply to
    both -- they are about whether a row is a topic at all, not about ordering.
    """

    def get(self, request):
        window = window_from(request)
        handles = accounts_from(request)
        dimension = str(request.query_params.get("dimension") or "hashtags").lower()
        if dimension not in {"hashtags", "phrases", "both"}:
            dimension = "hashtags"
        order = "volume" if str(request.query_params.get("rank") or "").lower() == "volume" else "surging"
        if connection.vendor != "postgresql":
            return Response({"results": [], "dimension": dimension, "rank": order})

        candidates: list[topics.TermStats] = []
        if dimension in {"hashtags", "both"}:
            candidates += _hashtag_topics(window, handles)
        if dimension in {"phrases", "both"}:
            candidates += _phrase_topics(window, handles)
        total_docs, previous_total_docs = _document_totals(window, handles)

        return Response({
            "dimension": dimension,
            "rank": order,
            "since": window.since.isoformat(),
            "until": window.until.isoformat(),
            # The denominators, so the console can state a rate rather than
            # asking the reader to trust a bare score.
            "total_docs": total_docs,
            "previous_total_docs": previous_total_docs,
            "results": topics.rank_terms(
                candidates,
                total_docs=total_docs,
                previous_total_docs=previous_total_docs,
                blocklist=topic_blocklist(),
                order=order,
                limit=_TOPIC_LIMIT,
            ),
        })


class TopicBlocklistView(APIView):
    """Hide or unhide a mined term.

    The scoring gets rid of filler on its own; this covers the residue no
    statistic can catch -- a boilerplate phrase every account in this particular
    roster happens to use, which is genuinely unusual and genuinely useless.
    """

    permission_classes = [IsStaff]

    def get(self, request):
        return Response({"terms": sorted(topic_blocklist())})

    def post(self, request):
        term = str(request.data.get("topic") or "").strip().lower()
        if not term:
            return Response({"detail": "topic required"}, status=400)
        terms = topic_blocklist()
        # One endpoint, both directions: the console's control is a toggle, and
        # two endpoints for one boolean is two things to keep in step.
        terms.discard(term) if request.data.get("hidden") is False else terms.add(term)
        namespace, name = _BLOCKLIST_STATE
        KeyValueState.objects.update_or_create(
            namespace=namespace, name=name, defaults={"data": {"terms": sorted(terms)}}
        )
        return Response({"terms": sorted(terms)})


class AccountsAnalyticsView(APIView):
    def get(self, request):
        window = window_from(request)
        rows = (
            _in_window(
                Tweet.objects.filter(
                    account__in=TwitterUser.objects.filter(tracking=True).values("handle")
                ),
                window,
            )
            .values("account")
            .annotate(
                posts=Count("id"),
                average_engagement=Avg(engagement_expression()),
                total_engagement=Sum(engagement_expression()),
                replies=Sum("replies"),
            )
            .order_by("-average_engagement", "account")[:100]
        )
        return Response({"results": [
            {
                "account": row["account"],
                "posts": row["posts"],
                "average_engagement": round(float(row["average_engagement"] or 0), 2),
                "total_engagement": int(row["total_engagement"] or 0),
                "replies": int(row["replies"] or 0),
            }
            for row in rows
        ]})


# A self-join over the tweet table is quadratic, so the honest lever is how many
# rows it is allowed to see. 1200 candidates is ~700k pairs, which the prefilters
# below cut to a small fraction and Postgres answers in about a second.
NARRATIVE_CANDIDATE_CAP = 1200
# Hard ceiling enforced by the database. This query used to run unbounded, blow
# past gunicorn's request timeout on any range over 24h, and take the worker down
# with it -- one click could kill a worker, and retrying killed the next one.
# Failing loudly in 15s is strictly better than a dead worker.
NARRATIVE_TIMEOUT_MS = 15_000


class NarrativesView(APIView):
    """Flag near-duplicate tweets from *different* accounts, posted close together."""

    def get(self, request):
        window = window_from(request)
        propagation_hours = _int_param(request, "window_hours", 24, 1, 168)
        similarity_threshold = _float_param(request, "similarity", 0.55, 0.1, 1.0)
        min_length = _int_param(request, "min_length", 40, 1, 500)
        limit = _int_param(request, "limit", 100, 1, 500)
        candidate_cap = _int_param(
            request, "candidates", NARRATIVE_CANDIDATE_CAP, 100, 5000
        )
        if connection.vendor != "postgresql":
            return Response({"results": []})
        handles = accounts_from(request)
        account_filter = "AND account = ANY(%s)" if handles else ""
        params = [window.since, window.until, min_length]
        if handles:
            params.append(handles)
        params += [candidate_cap, propagation_hours, similarity_threshold, limit]
        try:
            # SET LOCAL is scoped to a transaction, so the atomic block is what
            # makes the timeout real rather than a no-op under autocommit.
            with transaction.atomic(), connection.cursor() as cursor:
                # Postgres will not accept a bind parameter after SET, so the
                # value is interpolated -- safe here and only here because it is
                # a module-level int this file owns, never request input.
                cursor.execute(f"SET LOCAL statement_timeout = {int(NARRATIVE_TIMEOUT_MS)}")
                cursor.execute(
                    f"""
                    WITH candidates AS (
                        SELECT id, account, tweet_id, created_at,
                               lower(COALESCE(NULLIF(text_clean, ''), text)) AS body,
                               length(COALESCE(NULLIF(text_clean, ''), text)) AS len
                        FROM tweets_tweet
                        WHERE created_at >= %s
                          AND created_at <= %s
                          AND length(text) >= %s
                          {account_filter}
                        ORDER BY created_at DESC
                        LIMIT %s
                    )
                    SELECT first.account, first.tweet_id, first.created_at,
                           follower.account, follower.tweet_id, follower.created_at,
                           similarity(first.body, follower.body) AS score
                    FROM candidates first
                    JOIN candidates follower
                      ON follower.id <> first.id
                     -- The whole point of the panel: propagation BETWEEN accounts.
                     -- Without this, 9 in 10 results were one newsroom's own
                     -- reruns of its own headline matching itself.
                     -- (Keep percent signs out of this string entirely: the
                     -- driver scans the whole query, comments included, for
                     -- placeholders. test_raw_sql_placeholders.py enforces it.)
                     AND follower.account <> first.account
                     AND first.created_at <= follower.created_at
                     AND follower.created_at <= first.created_at + (%s || ' hours')::interval
                     -- Cheap prefilter: trigram similarity cannot clear the
                     -- threshold when the lengths are wildly different, and
                     -- length comparison costs nothing next to similarity().
                     AND follower.len BETWEEN first.len / 2 AND first.len * 2
                     AND similarity(first.body, follower.body) >= %s
                    ORDER BY score DESC, first.created_at DESC
                    LIMIT %s
                    """,
                    params,
                )
                rows = cursor.fetchall()
        except OperationalError:
            return Response(
                {
                    "detail": (
                        "Narrative detection timed out for this range. Try a shorter "
                        "range, a higher similarity, or fewer accounts."
                    ),
                    "results": [],
                },
                status=503,
            )
        return Response({"results": [
            {
                "first": {"account": first_account, "tweet_id": first_id, "created_at": first_at},
                "follower": {"account": follower_account, "tweet_id": follower_id, "created_at": follower_at},
                "similarity": round(float(score), 3),
            }
            for first_account, first_id, first_at, follower_account, follower_id, follower_at, score in rows
        ]})
