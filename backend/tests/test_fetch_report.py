"""fetch_report aggregates the three buckets for the 24h soak.

Unit for the --since parser (pure string → timedelta). Integration for the
report itself: it is an ORM boundary over FetchRun / Tweet / EndpointState,
which is where a wrong filter would hide a starved live bucket.
"""
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from fetching.management.commands.fetch_report import build_report, parse_since, render
from tweets.models import EndpointState, FetchRun, Search, Tweet, TwitterUser


def test_parse_since_accepts_hours_minutes_days():
    assert parse_since("24h") == timedelta(hours=24)
    assert parse_since("30m") == timedelta(minutes=30)
    assert parse_since("1d") == timedelta(days=1)


def test_parse_since_rejects_junk():
    with pytest.raises(ValueError):
        parse_since("yesterday")


@pytest.mark.django_db
def test_report_splits_buckets_and_lists_unfinished_archives():
    now = timezone.now()
    TwitterUser.objects.create(handle="jack", tracking=True)
    TwitterUser.objects.create(handle="elon", tracking=True)
    EndpointState.objects.create(
        account="jack",
        endpoint="UserTweets",
        data={"backfill_complete": True, "backfill_pages_done": 40},
    )
    EndpointState.objects.create(
        account="elon",
        endpoint="UserTweets",
        data={
            "backfill_complete": False,
            "backfill_pages_done": 12,
            "backfill_last_outcome": "paused_for_quota",
        },
    )
    FetchRun.objects.create(
        run_id="live-1",
        subsystem="live",
        status="completed",
        target="all",
        summary={
            "raw_pages": 6,
            "ingested_tweets": 10,
            "reports": [{"deferred": 0, "eligible": 2}],
            "recent_events": [{"type": "run_start", "accounts": ["jack", "elon"]}],
        },
    )
    FetchRun.objects.create(
        run_id="hist-1",
        subsystem="historical",
        status="partial",
        target="chunk:1",
        summary={
            "raw_pages": 25,
            "ingested_tweets": 400,
            "recent_events": [{"type": "run_start", "accounts": ["elon"]}],
        },
    )
    FetchRun.objects.create(
        run_id="search-1",
        subsystem="search",
        status="completed",
        target="war:Latest",
        summary={"raw_pages": 4, "ingested_tweets": 80},
    )
    Tweet.objects.create(
        dedup_key="1:fresh",
        tweet_id="fresh",
        account="jack",
        source_endpoint="UserTweets",
        created_at=now - timedelta(hours=1),
    )
    Tweet.objects.create(
        dedup_key="1:old",
        tweet_id="old",
        account="elon",
        source_endpoint="UserTweets",
        created_at=now - timedelta(days=3),
    )
    Tweet.objects.create(
        dedup_key="1:search",
        tweet_id="s1",
        account="random",
        source_endpoint="SearchTimeline",
        created_at=now - timedelta(hours=2),
    )
    Search.objects.create(name="war", slug="war", raw_query="war", last_run_at=now)

    report = build_report(since=now - timedelta(hours=24), now=now)

    assert report["tracked"] == 2
    assert report["live"]["pages"] == 6
    assert report["live"]["upserted"] == 10
    assert report["live"]["first_seen"] == 1
    assert report["live"]["deferred"] == 0
    assert report["live"]["polled"] == ["elon", "jack"]
    assert report["historical"]["pages"] == 25
    assert report["historical"]["first_seen"] == 1
    assert report["search"]["upserted"] == 80
    assert report["search"]["first_seen"] == 1
    assert report["archive"]["complete"] == 1
    assert report["archive"]["walking"] == [
        {"handle": "elon", "pages": 12, "outcome": "paused_for_quota"}
    ]
    text = render(report)
    assert "LIVE" in text
    assert "@elon  12p  paused_for_quota" in text
    assert "complete=1/2" in text


@pytest.mark.django_db
def test_call_command_prints_the_report():
    TwitterUser.objects.create(handle="jack", tracking=True)
    FetchRun.objects.create(
        run_id="live-cmd",
        subsystem="live",
        status="completed",
        summary={"raw_pages": 1, "ingested_tweets": 0},
    )
    call_command("fetch_report", "--since", "24h")
