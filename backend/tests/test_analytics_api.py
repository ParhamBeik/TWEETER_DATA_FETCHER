from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.db import connection
from rest_framework.test import APIClient

from fetching.ingest import upsert_tweet
from tweets.models import TwitterUser


@pytest.fixture
def client(db):
    user = User.objects.create_user(username="analytics", password="pw")
    api = APIClient()
    api.force_authenticate(user=user)
    return api


@pytest.mark.django_db
def test_overview_and_empty_analytics_are_available(client):
    TwitterUser.objects.create(handle="jack", tracking=True)
    upsert_tweet({
        "rest_id": "1", "author_id": "1", "account": "jack",
        "text": "A useful archive tweet", "created_at": "Wed Oct 10 20:19:24 +0000 2018",
    })

    overview = client.get("/api/stats/overview/")

    assert overview.status_code == 200
    assert overview.data["tweets"] == 1
    for path in (
        "/api/analytics/velocity/",
        "/api/analytics/topics/",
        "/api/analytics/accounts/",
        "/api/analytics/narratives/",
    ):
        assert client.get(path).status_code == 200


@pytest.mark.django_db
def test_ingestion_series_splits_capture_by_subsystem(client):
    from fetching.ingest import ingest_tweets

    TwitterUser.objects.create(handle="jack", tracking=True)
    ingest_tweets([{
        "rest_id": "1", "author_id": "1", "account": "jack", "text": "live one",
        "created_at": "Wed Oct 10 20:19:24 +0000 2018",
    }], "live")
    ingest_tweets([{
        "rest_id": "2", "author_id": "2", "account": "jack", "text": "backfilled",
        "created_at": "Wed Oct 10 20:19:24 +0000 2018",
    }], "historical")

    body = client.get("/api/analytics/ingestion/?range=24h").data

    assert body["bucket"] == "hour"
    assert body["totals"]["by_subsystem"] == {"live": 1, "historical": 1}
    assert body["totals"]["captured"] == 2
    assert {row["source_subsystem"] for row in body["captured"]} == {"live", "historical"}


@pytest.mark.django_db
def test_ingestion_bucket_widens_to_days_for_long_ranges(client):
    assert client.get("/api/analytics/ingestion/?range=30d").data["bucket"] == "day"
    assert client.get("/api/analytics/ingestion/?range=24h").data["bucket"] == "hour"
    # An explicit bucket wins over the automatic choice.
    assert client.get("/api/analytics/ingestion/?range=30d&bucket=hour").data["bucket"] == "hour"


@pytest.mark.django_db
def test_window_is_clamped_and_survives_nonsense(client):
    """A hand-edited range must not turn into an unbounded table scan."""
    from datetime import timedelta

    from django.utils import timezone

    body = client.get("/api/analytics/ingestion/?range=9999d").data
    span = timezone.now() - timezone.datetime.fromisoformat(body["since"])
    assert span <= timedelta(days=91)

    # Garbage falls back to the default window rather than 500ing.
    assert client.get("/api/analytics/ingestion/?range=banana").status_code == 200
    assert client.get("/api/analytics/ingestion/?since=not-a-date").status_code == 200


@pytest.mark.django_db
def test_pipeline_reports_quota_cadence_and_backfill(client):
    from django.utils import timezone

    from tweets.models import FetchRun, KeyValueState

    TwitterUser.objects.create(handle="jack", tracking=True)
    TwitterUser.objects.create(
        handle="ghost", tracking=True, quarantined=True, quarantine_reason="dead handle"
    )
    KeyValueState.objects.create(
        namespace="request_state",
        name="historical_live:rate_limits.json",
        data={"UserTweets": {"remaining": 12, "limit": 50, "reset": int(timezone.now().timestamp()) + 300}},
    )
    KeyValueState.objects.create(
        namespace="request_state",
        name="historical_live:endpoint_health.json",
        data={"UserTweets": "healthy"},
    )
    FetchRun.objects.create(run_id="r1", subsystem="live", status="completed", summary={"ingested_tweets": 7})

    body = client.get("/api/stats/pipeline/").data

    quota = {row["endpoint"]: row for row in body["rate_limits"]}
    assert quota["UserTweets"]["remaining"] == 12
    assert 0 < quota["UserTweets"]["resets_in_seconds"] <= 300
    assert body["endpoint_health"]["UserTweets"] == "healthy"

    live = next(row for row in body["subsystems"] if row["subsystem"] == "live")
    assert live["last_run"]["ingested_tweets"] == 7
    assert live["interval_seconds"] > 0

    assert body["archive"]["tracked"] == 2
    assert [row["handle"] for row in body["quarantined"]] == ["ghost"]


@pytest.mark.django_db
def test_analytics_accept_the_shared_window_and_account_filters(client):
    TwitterUser.objects.create(handle="jack", tracking=True)
    query = "range=7d&bucket=day&account=jack&account=elon"
    for path in (
        "/api/analytics/velocity/",
        "/api/analytics/topics/",
        "/api/analytics/accounts/",
        "/api/analytics/narratives/",
        "/api/analytics/ingestion/",
    ):
        assert client.get(f"{path}?{query}").status_code == 200
    assert client.get("/api/analytics/topics/?dimension=phrases").status_code == 200
    assert client.get("/api/analytics/topics/?dimension=both").status_code == 200


postgres_only = pytest.mark.skipif(
    "connection.vendor != 'postgresql'",
    reason="topic mining and velocity deltas are raw Postgres SQL",
)


@pytest.mark.django_db
@postgres_only
def test_phrase_mining_finds_untagged_topics_and_keeps_word_order(client):
    from fetching.ingest import ingest_tweets

    TwitterUser.objects.create(handle="jack", tracking=True)
    ingest_tweets([
        {
            "rest_id": str(n), "author_id": "1", "account": "jack",
            "text": "the central bank raised interest rates again https://x.com/a @someone",
            "created_at": "Wed Oct 10 20:19:24 +0000 2018",
            "entities": {"hashtags": ["economy"]},
        }
        for n in range(3)
    ], "live")

    body = client.get(
        "/api/analytics/topics/?dimension=both&since=2018-01-01T00:00:00Z"
        "&until=2019-01-01T00:00:00Z"
    ).data
    topics = {row["topic"]: row for row in body["results"]}

    # Hashtags still counted, and given their own kind.
    assert topics["economy"]["kind"] == "hashtag"
    # Untagged discussion is now visible as single words...
    assert "interest" in topics
    # ...and as bigrams in the order they were actually written.
    assert "interest rates" in topics
    assert "rates interest" not in topics
    # Stopwords, the URL and the @mention are stripped.
    assert "the" not in topics
    assert not any("http" in topic for topic in topics)
    assert "someone" not in topics


@pytest.mark.django_db
@postgres_only
def test_velocity_reports_engagement_gained_not_absolute_totals(client):
    from django.utils import timezone

    from fetching.ingest import upsert_tweet
    from tweets.models import TweetMetric

    TwitterUser.objects.create(handle="jack", tracking=True)
    steady = upsert_tweet({
        "rest_id": "steady", "author_id": "1", "account": "jack", "text": "old news",
        "created_at": "Wed Oct 10 20:19:24 +0000 2018", "likes": 5000,
    })
    climbing = upsert_tweet({
        "rest_id": "climbing", "author_id": "1", "account": "jack", "text": "breaking",
        "created_at": "Wed Oct 10 20:19:24 +0000 2018", "likes": 10,
    })
    # ingest writes a snapshot of its own on first sight; drop those so the
    # timeline below is exactly the two points each tweet is meant to have.
    TweetMetric.objects.all().delete()
    now = timezone.now()
    # A huge but static tweet must not outrank a small, fast-moving one.
    for tweet, points in ((steady, [5000, 5000]), (climbing, [10, 900])):
        for offset, likes in enumerate(points):
            metric = TweetMetric.objects.create(tweet=tweet, likes=likes)
            TweetMetric.objects.filter(pk=metric.pk).update(
                captured_at=now - timedelta(minutes=30 - offset * 10)
            )

    body = client.get("/api/analytics/velocity/?range=24h").data

    assert [row["tweet_id"] for row in body["results"]] == ["climbing"]
    assert body["results"][0]["velocity"] == 890
    assert sum(row["gained"] for row in body["series"]) == 890


@pytest.mark.django_db
def test_accounts_analytics_excludes_retweets(client):
    from django.utils import timezone
    from tweets.models import Tweet

    TwitterUser.objects.create(handle="alice", tracking=True)
    # Alice original tweet with 10 likes
    Tweet.objects.create(
        dedup_key="alice:101",
        tweet_id="101",
        account="alice",
        type="Tweet",
        likes=10,
        retweets=0,
        replies=0,
        quotes=0,
        created_at=timezone.now(),
    )
    # Alice retweet of a viral post with 50,000 likes
    Tweet.objects.create(
        dedup_key="alice:102",
        tweet_id="102",
        account="alice",
        type="Retweet",
        likes=50000,
        retweets=5000,
        replies=0,
        quotes=0,
        created_at=timezone.now(),
    )

    response = client.get("/api/analytics/accounts/?range=24h")
    assert response.status_code == 200
    rows = response.data["results"]
    assert len(rows) == 1
    assert rows[0]["account"] == "alice"
    assert rows[0]["posts"] == 1  # Retweet excluded
    assert rows[0]["average_engagement"] == 10.0  # Not inflated to 25,000+


@pytest.mark.django_db
def test_overview_respects_until_boundary(client):
    from django.utils import timezone
    from tweets.models import Tweet

    now = timezone.now()
    # Tweet from 5 days ago
    Tweet.objects.create(
        dedup_key="jack:201",
        tweet_id="201", account="jack", type="Tweet",
        created_at=now - timedelta(days=5),
    )
    # Tweet from 1 day ago
    Tweet.objects.create(
        dedup_key="jack:202",
        tweet_id="202", account="jack", type="Tweet",
        created_at=now - timedelta(days=1),
    )

    # Window from 6 days ago until 3 days ago
    since = (now - timedelta(days=6)).isoformat()
    until = (now - timedelta(days=3)).isoformat()

    response = client.get(f"/api/stats/overview/?since={since}&until={until}")
    assert response.status_code == 200
    assert response.data["tweets"] == 2
    assert response.data["tweets_in_window"] == 1  # Only tweet 201 falls in window


@pytest.mark.django_db
def test_calendar_start_week_starts_on_saturday():
    from tweets.views import _calendar_start

    start = _calendar_start("week")
    # Saturday in python weekday() is 5
    from fetcher.processing import TZ as FEED_TZ
    local_start = start.astimezone(FEED_TZ)
    assert local_start.weekday() == 5  # Saturday
    assert local_start.hour == 0
    assert local_start.minute == 0


@pytest.mark.django_db
def test_case_insensitive_login(client):
    User.objects.create_user(username="ParhamBeik", password="secretpassword123")
    anon = APIClient()

    # Login with lower case
    response = anon.post("/api/auth/login/", {"username": "parhambeik", "password": "secretpassword123"})
    assert response.status_code == 200
    assert response.data["user"]["username"] == "ParhamBeik"

