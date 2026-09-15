"""Read-only archive analytics backed by the existing Postgres tables.

Everything here shares one window contract:

    ?range=24h|7d|30d|90d      (or ?since=&until= ISO timestamps)
    ?bucket=auto|hour|day|week (auto: hourly under 48h, daily above)
    ?account=a&account=b       (repeatable; omitted means every account)

so the console can drive Pulse, Feed and Analyze from a single filter bar.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction, OperationalError
from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from fetching.accounts import archive_progress, silent_accounts
from fetching.health import queue_health

from tweets import topics
from tweets.models import FetchRun, KeyValueState, Search, SearchTweet, Tweet, TwitterUser
from tweets.params import body_mapping
from tweets.permissions import IsStaff
from tweets.serializers import TweetSerializer, _new_tweets
from tweets.windows import BUCKET_KINDS, Window, accounts_from, window_from
from tweets.stats import (
    _BLOCKLIST_STATE,
    _document_totals,
    _endpoint_health,
    _engagement_sql,
    _for_accounts,
    _hashtag_topics,
    _in_window,
    _phrase_topics,
    _rate_limits,
    _request_spend,
    _series,
    _SUBSYSTEM_INTERVALS,
    _TOPIC_LIMIT,
    METRIC_ENGAGEMENT_FIELDS,
    SUBSYSTEMS,
    topic_blocklist,
)

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
            in_window=Count("id", filter=Q(created_at__gte=window.since, created_at__lte=window.until)),
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


class IngestionView(APIView):
    """The Pulse time series: what arrived, from where, and what it cost."""

    throttle_scope = "analytics"

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
        # captured_search above is this same COUNT over SearchTweet; it used to
        # be issued twice per request to the Pulse dashboard, which polls.
        if captured_search:
            by_subsystem["search"] = by_subsystem.get("search", 0) + captured_search

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
class PipelineView(APIView):
    """Point-in-time collector state: quota, cadence, backfill, what's running."""

    def get(self, request):
        now = timezone.now()
        # Resolved once and reused for the per-subsystem counts below. This view
        # is polled every 20 seconds by the budget rail on every page, so three
        # extra COUNT queries for a list already being fetched is not free.
        running = list(
            FetchRun.objects.filter(status="running").values(
                "run_id", "subsystem", "target", "started_at"
            )
        )
        running_by_subsystem = Counter(row["subsystem"] for row in running)
        subsystems = []
        for name in SUBSYSTEMS:
            last = FetchRun.objects.filter(subsystem=name).exclude(status="running").first()
            interval = int(getattr(settings, _SUBSYSTEM_INTERVALS[name]))
            due_at = last.started_at + timedelta(seconds=interval) if last else None
            subsystems.append({
                "subsystem": name,
                "interval_seconds": interval,
                "running": running_by_subsystem.get(name, 0),
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
        silent = silent_accounts(now=now)
        return Response({
            "now": now.isoformat(),
            "subsystems": subsystems,
            "rate_limits": _rate_limits(),
            "endpoint_health": _endpoint_health(),
            "queues": queue_health(),
            "running": running,
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
            # Accounts every poll reports as healthy that have stopped producing
            # anything. Without this the console showed 41 days of green for an
            # account whose timeline had been frozen the whole time.
            "silent": silent[:12],
            "silent_count": len(silent),
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

    # Window functions over the whole metric table; see the throttle rates.
    throttle_scope = "analytics"

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
        # One serializer over the whole ranking, not one per row: the media and
        # avatar lookups are batched per serializer instance, so building 50 of
        # them spent 50 lookups on what is one page of results.
        ordered = [tweets[tweet_id] for tweet_id in ids if tweet_id in tweets]
        data = []
        for tweet, row in zip(ordered, TweetSerializer(ordered, many=True).data):
            row["velocity"] = rates[tweet.id]
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

class TopicsView(APIView):
    """What is being discussed, ranked by what changed rather than what is common.

    `?rank=surging` (default) orders by how unusual a term's current rate is
    against the previous window of equal length; `?rank=volume` orders by how
    many posts mention it. The support, filler and nested-gram filters apply to
    both -- they are about whether a row is a topic at all, not about ordering.
    """

    # Phrase mining tokenizes and bigrams every post in the window.
    throttle_scope = "analytics"

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
        data = body_mapping(request)
        term = str(data.get("topic") or "").strip().lower()
        if not term:
            return Response({"detail": "topic required"}, status=400)
        terms = topic_blocklist()
        # One endpoint, both directions: the console's control is a toggle, and
        # two endpoints for one boolean is two things to keep in step.
        terms.discard(term) if data.get("hidden") is False else terms.add(term)
        namespace, name = _BLOCKLIST_STATE
        KeyValueState.objects.update_or_create(
            namespace=namespace, name=name, defaults={"data": {"terms": sorted(terms)}}
        )
        return Response({"terms": sorted(terms)})


class AccountsAnalyticsView(APIView):
    throttle_scope = "analytics"

    def get(self, request):
        window = window_from(request)
        handles = accounts_from(request)
        # Exclude retweets so accounts are ranked strictly on their own authored content,
        # rather than crediting other people's viral posts they happened to repost.
        scoped = _for_accounts(
            Tweet.objects.filter(
                account__in=TwitterUser.objects.filter(tracking=True).values("handle")
            ).exclude(type="Retweet"),
            handles,
        )
        rows = (
            _in_window(
                scoped,
                window,
            )
            .values("account")
            .annotate(
                posts=Count("id"),
                # The stored column, not the expression: same four fields, but
                # the database has already summed them on every row.
                average_engagement=Avg("engagement"),
                total_engagement=Sum("engagement"),
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

    # A trigram self-join, already carrying its own statement timeout because a
    # single unbounded request could take a worker down. Metered as well, so a
    # retry loop cannot simply keep re-triggering that.
    throttle_scope = "analytics"

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
                "first": {
                    "account": first_account,
                    "tweet_id": first_id,
                    "created_at": first_at,
                    "text": first_text,
                },
                "follower": {
                    "account": follower_account,
                    "tweet_id": follower_id,
                    "created_at": follower_at,
                    "text": follower_text,
                },
                "similarity": round(float(score), 3),
            }
            for first_account, first_id, first_at, first_text, follower_account, follower_id, follower_at, follower_text, score in rows
        ]})
