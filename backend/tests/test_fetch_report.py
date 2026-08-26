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
    # first_seen is attributed by source_subsystem, which ingest stamps on
    # insert. It used to be guessed from how old the tweet was, because live and
    # the archive walk both report source_endpoint="UserTweets".
    Tweet.objects.create(
        dedup_key="1:fresh",
        tweet_id="fresh",
        account="jack",
        source_endpoint="UserTweets",
        source_subsystem="live",
        created_at=now - timedelta(hours=1),
    )
    Tweet.objects.create(
        dedup_key="1:old",
        tweet_id="old",
        account="elon",
        source_endpoint="UserTweets",
        source_subsystem="historical",
        created_at=now - timedelta(days=3),
    )
    Tweet.objects.create(
        dedup_key="1:search",
        tweet_id="s1",
        account="random",
        source_endpoint="SearchTimeline",
        source_subsystem="search",
        created_at=now - timedelta(hours=2),
    )
    # A row ingested before source_subsystem existed belongs to no bucket rather
    # than being credited to whichever one the old heuristic happened to pick.
    Tweet.objects.create(
        dedup_key="1:legacy",
        tweet_id="legacy",
        account="jack",
        source_endpoint="UserTweets",
        created_at=now - timedelta(hours=3),
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
        {
            "handle": "elon",
            "priority": 7,
            "pages": 12,
            "stalled_ticks": 0,
            "outcome": "paused_for_quota",
            "quarantined": False,
        }
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


@pytest.mark.django_db
def test_exhausted_without_a_reason_is_depth_limited_not_complete():
    """Rows written before backfill_depth_reason existed still lie as complete."""
    from fetching.accounts import archive_progress

    TwitterUser.objects.create(handle="elonmusk", tracking=True)
    TwitterUser.objects.create(handle="jack", tracking=True)
    EndpointState.objects.create(
        account="elonmusk",
        endpoint="UserTweets",
        data={
            "backfill_complete": True,
            "backfill_last_outcome": "success_timeline_exhausted",
            "backfill_pages_done": 45,
        },
    )
    EndpointState.objects.create(
        account="jack",
        endpoint="UserTweets",
        data={
            "backfill_complete": True,
            "backfill_depth_reason": "reached_first_tweet",
            "backfill_last_outcome": "success_true_end",
        },
    )
    progress = archive_progress()
    assert progress["complete"] == ["jack"]
    assert progress["depth_limited"] == ["elonmusk"]
    assert progress["walking"] == []


@pytest.mark.django_db
def test_reopen_stamps_exhausted_rows_instead_of_re_walking_them():
    TwitterUser.objects.create(handle="elonmusk", tracking=True)
    TwitterUser.objects.create(handle="stuck", tracking=True)
    EndpointState.objects.create(
        account="elonmusk",
        endpoint="UserTweets",
        data={
            "backfill_complete": True,
            "backfill_last_outcome": "success_timeline_exhausted",
        },
    )
    EndpointState.objects.create(
        account="stuck",
        endpoint="UserTweets",
        data={
            "backfill_complete": True,
            "backfill_last_outcome": "paused_for_quota",
        },
    )
    call_command("reopen_shallow_archives")
    exhausted = EndpointState.objects.get(account="elonmusk").data
    stuck = EndpointState.objects.get(account="stuck").data
    assert exhausted["backfill_complete"] is True
    assert exhausted["backfill_depth_reason"] == "provider_depth_limit"
    assert stuck["backfill_complete"] is False
