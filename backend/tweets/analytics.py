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
from django.db import connection
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

from tweets.models import FetchRun, KeyValueState, Search, Tweet, TwitterUser
from tweets.serializers import TweetSerializer

# Single source of truth for "engagement": kept as field names so both the ORM
# expression below and the raw-SQL views can build the same formula from it.
ENGAGEMENT_FIELDS = ("likes", "retweets", "views")


def _engagement_sql(prefix: str = "") -> str:
    """The engagement formula as SQL, table-qualified when a query joins.

    tweets_tweet and tweets_tweetmetric both carry likes/retweets/views, so an
    unqualified sum is ambiguous the moment the two are joined.
    """
    return " + ".join(f"{prefix}{field}" for field in ENGAGEMENT_FIELDS)

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


def _series(qs, window: Window, field: str, *, group: str | None = None) -> list[dict]:
    """Bucketed counts, optionally split by a second column.

    ORM rather than raw SQL so this works on the SQLite test database too --
    only the JSON/trigram views below genuinely need Postgres.
    """
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

        previous = Window(since=window.previous_since, until=window.since, bucket=window.bucket)
        captured = _in_window(tweets, window, "ingested_at").count()
        captured_before = _in_window(tweets, previous, "ingested_at").count()

        runs = FetchRun.objects.filter(
            started_at__gte=window.since, started_at__lte=window.until
        )
        run_totals = runs.order_by().values("subsystem", "status").annotate(count=Count("id"))

        return Response({
            "since": window.since.isoformat(),
            "until": window.until.isoformat(),
            "bucket": window.bucket,
            # Who is doing the collecting, bucketed over the window.
            "captured": _series(tweets, window, "ingested_at", group="source_subsystem"),
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
                "captured_previous": captured_before,
                "captured_delta": captured - captured_before,
                "by_subsystem": {
                    row["source_subsystem"] or "unknown": row["count"]
                    for row in _in_window(tweets, window, "ingested_at")
                    .order_by()
                    .values("source_subsystem")
                    .annotate(count=Count("id"))
                },
                "archive_total": Tweet.objects.count(),
                "oldest_tweet": (
                    Tweet.objects.filter(created_at__isnull=False)
                    .order_by("created_at")
                    .values_list("created_at", flat=True)
                    .first()
                ),
            },
        })


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
            if not isinstance(state, dict):
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
            health.update({str(k): str(v) for k, v in row.data.items()})
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
        engagement = _engagement_sql("m.")
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


def _hashtag_topics(window: Window, handles: list[str]) -> list[tuple[str, int, int]]:
    account_filter = "AND account = ANY(%s)" if handles else ""
    params = [window.previous_since, window.until]
    if handles:
        params.append(handles)
    params += [window.since, window.since, window.since]
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            WITH tags AS (
                SELECT lower(tag) AS topic, created_at
                FROM tweets_tweet
                CROSS JOIN LATERAL jsonb_array_elements_text(
                    COALESCE(entities->'hashtags', '[]'::jsonb)
                ) AS tag
                WHERE created_at >= %s AND created_at <= %s
                {account_filter}
            )
            SELECT topic,
                   count(*) FILTER (WHERE created_at >= %s) AS current_count,
                   count(*) FILTER (WHERE created_at < %s) AS previous_count
            FROM tags
            GROUP BY topic
            HAVING count(*) FILTER (WHERE created_at >= %s) > 0
            ORDER BY current_count DESC, topic
            LIMIT {_TOPIC_LIMIT}
            """,
            params,
        )
        return [(topic, int(current), int(previous)) for topic, current, previous in cursor.fetchall()]


def _phrase_topics(window: Window, handles: list[str]) -> list[tuple[str, int, int]]:
    """Frequent words and two-word phrases mined from tweet text.

    Tokens come from splitting on non-alphanumerics rather than from
    to_tsvector: a tsvector is stored sorted by lexeme, so WITH ORDINALITY over
    one gives alphabetical neighbours and the bigram join would pair words that
    never appeared together. regexp_split_to_array preserves document order,
    and [:alnum:] is unicode-aware on a UTF-8 database, so non-Latin scripts
    survive the split intact.

    URLs, @mentions and #hashtags are stripped first -- hashtags are counted by
    their own dimension, and a URL is one enormous meaningless token.
    """
    account_filter = "AND account = ANY(%s)" if handles else ""
    params = [window.previous_since, window.until]
    if handles:
        params.append(handles)
    params += [_STOPWORDS, _MIN_TOKEN_LENGTH, window.since, window.since, window.since]
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            WITH scoped AS (
                SELECT id, created_at, text
                FROM tweets_tweet
                WHERE created_at >= %s AND created_at <= %s
                {account_filter}
            ), tokens AS (
                SELECT s.id, s.created_at, token.lexeme, token.position
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
                SELECT lexeme AS topic, created_at FROM tokens
                UNION ALL
                SELECT a.lexeme || ' ' || b.lexeme AS topic, a.created_at
                FROM tokens a
                JOIN tokens b ON b.id = a.id AND b.position = a.position + 1
            )
            SELECT topic,
                   count(*) FILTER (WHERE created_at >= %s) AS current_count,
                   count(*) FILTER (WHERE created_at < %s) AS previous_count
            FROM phrases
            GROUP BY topic
            HAVING count(*) FILTER (WHERE created_at >= %s) > 1
            ORDER BY current_count DESC, topic
            LIMIT {_TOPIC_LIMIT}
            """,
            params,
        )
        return [(topic, int(current), int(previous)) for topic, current, previous in cursor.fetchall()]


class TopicsView(APIView):
    """What is being discussed: hashtags, mined phrases, or both."""

    def get(self, request):
        window = window_from(request)
        handles = accounts_from(request)
        dimension = str(request.query_params.get("dimension") or "hashtags").lower()
        if dimension not in {"hashtags", "phrases", "both"}:
            dimension = "hashtags"
        if connection.vendor != "postgresql":
            return Response({"results": [], "dimension": dimension})

        rows: list[tuple[str, int, int, str]] = []
        if dimension in {"hashtags", "both"}:
            rows += [(t, c, p, "hashtag") for t, c, p in _hashtag_topics(window, handles)]
        if dimension in {"phrases", "both"}:
            rows += [(t, c, p, "phrase") for t, c, p in _phrase_topics(window, handles)]
        rows.sort(key=lambda row: (-row[1], row[0]))
        rows = rows[:_TOPIC_LIMIT]

        return Response({
            "dimension": dimension,
            "since": window.since.isoformat(),
            "until": window.until.isoformat(),
            "results": [
                {
                    "topic": topic,
                    "kind": kind,
                    "current_count": current,
                    "previous_count": previous,
                    "delta": current - previous,
                }
                for topic, current, previous, kind in rows
            ],
        })


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


class NarrativesView(APIView):
    """Flag near-duplicate tweets posted within a propagation window of each other."""

    def get(self, request):
        window = window_from(request)
        propagation_hours = _int_param(request, "window_hours", 24, 1, 168)
        similarity_threshold = _float_param(request, "similarity", 0.55, 0.1, 1.0)
        min_length = _int_param(request, "min_length", 40, 1, 500)
        if connection.vendor != "postgresql":
            return Response({"results": []})
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT first.account, first.tweet_id, first.created_at,
                       follower.account, follower.tweet_id, follower.created_at,
                       similarity(lower(first.text), lower(follower.text)) AS similarity
                FROM tweets_tweet first
                JOIN tweets_tweet follower
                  ON first.id < follower.id
                 AND first.created_at <= follower.created_at
                 AND follower.created_at <= first.created_at + (%s || ' hours')::interval
                 AND similarity(lower(first.text), lower(follower.text)) >= %s
                WHERE first.created_at >= %s
                  AND first.created_at <= %s
                  AND length(first.text) >= %s
                  AND length(follower.text) >= %s
                ORDER BY first.created_at, similarity DESC
                LIMIT 100
                """,
                [
                    propagation_hours,
                    similarity_threshold,
                    window.since,
                    window.until,
                    min_length,
                    min_length,
                ],
            )
            rows = cursor.fetchall()
        return Response({"results": [
            {
                "first": {"account": first_account, "tweet_id": first_id, "created_at": first_at},
                "follower": {"account": follower_account, "tweet_id": follower_id, "created_at": follower_at},
                "similarity": round(float(score), 3),
            }
            for first_account, first_id, first_at, follower_account, follower_id, follower_at, score in rows
        ]})
