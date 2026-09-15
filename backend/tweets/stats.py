"""The queries behind the analytics endpoints.

Everything here answers a question about the archive and returns plain Python:
counts, buckets, ranked terms, endpoint state. None of it knows what HTTP is.
The matching views in tweets/analytics.py parse the request, call in here, and
serialize -- that split is the only reason a 1000-line module holding views,
parameter coercion and hand-written SQL together is now readable.

Two rules this module lives by, both load-bearing:

  * A request value never reaches a SQL string. Bucket granularity is looked up
    in windows.BUCKET_KINDS and handles go in as a parameter; the f-strings here
    interpolate only module constants. tests/test_raw_sql_placeholders.py
    enforces the related rule about percent signs.
  * The raw-SQL paths are PostgreSQL-only and their callers check
    `connection.vendor` before entering. On SQLite -- which is what a local
    pytest run uses unless TEST_POSTGRES_HOST is set -- they are never reached,
    so a green local run says nothing about them. Run against Postgres.
"""
from __future__ import annotations


from django.db import connection, transaction
from django.db.models import Count
from django.db.models.functions import Trunc
from django.utils import timezone

from tweets import topics
from tweets.models import KeyValueState, Tweet
from tweets.windows import BUCKET_KINDS, Window

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


SUBSYSTEMS = ("live", "historical", "search")


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


# --- Narratives -------------------------------------------------------------

NARRATIVE_CANDIDATE_CAP = 1200
# Hard ceiling enforced by the database. This query used to run unbounded, blow
# past gunicorn's request timeout on any range over 24h, and take the worker down
# with it -- one click could kill a worker, and retrying killed the next one.
# Failing loudly in 15s is strictly better than a dead worker.
NARRATIVE_TIMEOUT_MS = 15_000


def narrative_pairs(
    window: Window,
    handles: list[str],
    *,
    min_length: int,
    candidate_cap: int,
    propagation_hours: int,
    similarity_threshold: float,
    limit: int,
):
    """Near-duplicate tweet pairs from different accounts, posted close together.

    Raises OperationalError when the statement timeout fires; the caller turns
    that into the 503 the console knows how to explain.
    """
    account_filter = "AND account = ANY(%s)" if handles else ""
    params = [window.since, window.until, min_length]
    if handles:
        params.append(handles)
    params += [candidate_cap, propagation_hours, similarity_threshold, limit]
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
            SELECT first.account, first.tweet_id, first.created_at, first.body,
                   follower.account, follower.tweet_id, follower.created_at, follower.body,
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
        return cursor.fetchall()


# --- Velocity ---------------------------------------------------------------


def velocity_rankings(window: Window, handles: list[str]):
    """Engagement gained during the window: the top 50 tweets, and its shape.

    Returns (rows, series): rows are (tweet_id, velocity) ranked, series are
    (bucket, gained, tweets) across the whole window.
    """
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
    return rows, series
