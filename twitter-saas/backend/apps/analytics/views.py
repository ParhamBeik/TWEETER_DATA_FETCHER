"""Read-only archive analytics backed by the existing Postgres tables."""
from __future__ import annotations

from datetime import timedelta

from django.db import connection
from django.db.models import Avg, Count, Sum
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tweets.models import FetchRun, Tweet, TwitterUser
from apps.tweets.serializers import TweetSerializer


def _hours(request, default: int = 24) -> int:
    try:
        return max(1, min(168, int(request.query_params.get("hours", default))))
    except (TypeError, ValueError):
        return default


class OverviewView(APIView):
    def get(self, request):
        cutoff = timezone.now() - timedelta(hours=_hours(request))
        latest = list(FetchRun.objects.all()[:5].values(
            "subsystem", "status", "started_at", "finished_at", "summary"
        ))
        return Response({
            "tweets": Tweet.objects.count(),
            "tweets_in_window": Tweet.objects.filter(created_at__gte=cutoff).count(),
            "tracked_accounts": TwitterUser.objects.filter(tracking=True).count(),
            "quarantined_accounts": TwitterUser.objects.filter(quarantined=True).count(),
            "latest_runs": latest,
        })


class VelocityView(APIView):
    """Rank tweets by engagement gained during the requested metric window."""

    def get(self, request):
        cutoff = timezone.now() - timedelta(hours=_hours(request))
        if connection.vendor != "postgresql":
            return Response({"results": []})
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH points AS (
                    SELECT tweet_id, likes, retweets, views, captured_at,
                           row_number() OVER (PARTITION BY tweet_id ORDER BY captured_at) AS first_n,
                           row_number() OVER (PARTITION BY tweet_id ORDER BY captured_at DESC) AS last_n
                    FROM tweets_tweetmetric
                    WHERE captured_at >= %s
                ), deltas AS (
                    SELECT tweet_id,
                           max(likes + retweets + views) FILTER (WHERE last_n = 1)
                         - max(likes + retweets + views) FILTER (WHERE first_n = 1) AS velocity
                    FROM points
                    GROUP BY tweet_id
                    HAVING count(*) >= 2
                )
                SELECT tweet_id, velocity
                FROM deltas
                ORDER BY velocity DESC, tweet_id DESC
                LIMIT 50
                """,
                [cutoff],
            )
            rows = cursor.fetchall()
        ids = [row[0] for row in rows]
        rates = {row[0]: int(row[1] or 0) for row in rows}
        tweets = {tweet.id: tweet for tweet in Tweet.objects.filter(id__in=ids).select_related("author")}
        data = []
        for tweet_id in ids:
            if tweet := tweets.get(tweet_id):
                row = TweetSerializer(tweet).data
                row["velocity"] = rates[tweet_id]
                data.append(row)
        return Response({"results": data})


class TopicsView(APIView):
    def get(self, request):
        hours = _hours(request)
        cutoff = timezone.now() - timedelta(hours=hours)
        previous = cutoff - timedelta(hours=hours)
        if connection.vendor != "postgresql":
            return Response({"results": []})
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH tags AS (
                    SELECT lower(tag) AS topic, created_at
                    FROM tweets_tweet
                    CROSS JOIN LATERAL jsonb_array_elements_text(
                        COALESCE(entities->'hashtags', '[]'::jsonb)
                    ) AS tag
                    WHERE created_at >= %s
                )
                SELECT topic,
                       count(*) FILTER (WHERE created_at >= %s) AS current_count,
                       count(*) FILTER (WHERE created_at < %s) AS previous_count
                FROM tags
                GROUP BY topic
                HAVING count(*) FILTER (WHERE created_at >= %s) > 0
                ORDER BY current_count DESC, topic
                LIMIT 50
                """,
                [previous, cutoff, cutoff, cutoff],
            )
            rows = cursor.fetchall()
        return Response({"results": [
            {
                "topic": topic,
                "current_count": int(current_count),
                "previous_count": int(previous_count),
                "delta": int(current_count - previous_count),
            }
            for topic, current_count, previous_count in rows
        ]})


class AccountsAnalyticsView(APIView):
    def get(self, request):
        cutoff = timezone.now() - timedelta(hours=_hours(request))
        rows = (
            Tweet.objects.filter(created_at__gte=cutoff, account__in=TwitterUser.objects.filter(tracking=True).values("handle"))
            .values("account")
            .annotate(
                posts=Count("id"),
                average_engagement=Avg("likes") + Avg("retweets") + Avg("views"),
                replies=Sum("replies"),
            )
            .order_by("-average_engagement", "account")[:100]
        )
        return Response({"results": [
            {
                "account": row["account"],
                "posts": row["posts"],
                "average_engagement": round(float(row["average_engagement"] or 0), 2),
                "replies": int(row["replies"] or 0),
            }
            for row in rows
        ]})


class NarrativesView(APIView):
    def get(self, request):
        cutoff = timezone.now() - timedelta(hours=_hours(request))
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
                 AND follower.created_at <= first.created_at + interval '24 hours'
                 AND similarity(lower(first.text), lower(follower.text)) >= 0.55
                WHERE first.created_at >= %s
                  AND length(first.text) >= 40
                  AND length(follower.text) >= 40
                ORDER BY first.created_at, similarity DESC
                LIMIT 100
                """,
                [cutoff],
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
