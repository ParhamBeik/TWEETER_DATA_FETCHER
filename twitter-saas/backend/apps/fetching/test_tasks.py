"""Retention tasks: search-only TTL and FetchRun purge."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.fetching.ingest import upsert_tweet
from apps.fetching.tasks import (
    backfill_historical_all,
    poll_live_all,
    purge_expired_search_tweets,
    purge_old_fetch_runs,
    repoll_searches,
)
from apps.tweets.models import FetchRun, Search, SearchResult, Tweet, TwitterUser


@pytest.fixture(autouse=True)
def _clear_cycle_locks():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_purge_expired_search_tweets_keeps_tracked_accounts(settings):
    settings.SEARCH_TWEET_TTL_DAYS = 30
    TwitterUser.objects.create(handle="jack", tracking=True)
    search = Search.objects.create(name="ai", slug="ai", raw_query="ai")

    old_search_only = upsert_tweet(
        {
            "rest_id": "1",
            "author_id": "9",
            "account": "random",
            "text": "old search",
            "created_at": "Wed Oct 10 20:19:24 +0000 2018",
        }
    )
    SearchResult.objects.create(search=search, tweet=old_search_only, rank=0)
    Tweet.objects.filter(pk=old_search_only.pk).update(
        ingested_at=timezone.now() - timedelta(days=31)
    )

    kept_tracked = upsert_tweet(
        {
            "rest_id": "2",
            "author_id": "1",
            "account": "jack",
            "text": "old but tracked",
            "created_at": "Wed Oct 10 20:19:24 +0000 2018",
        }
    )
    SearchResult.objects.create(search=search, tweet=kept_tracked, rank=1)
    Tweet.objects.filter(pk=kept_tracked.pk).update(
        ingested_at=timezone.now() - timedelta(days=31)
    )

    fresh_search = upsert_tweet(
        {
            "rest_id": "3",
            "author_id": "8",
            "account": "fresh",
            "text": "fresh search",
            "created_at": "Wed Oct 10 20:19:24 +0000 2018",
        }
    )
    SearchResult.objects.create(search=search, tweet=fresh_search, rank=2)

    deleted = purge_expired_search_tweets()
    assert deleted >= 1
    assert not Tweet.objects.filter(tweet_id="1").exists()
    assert Tweet.objects.filter(tweet_id="2").exists()
    assert Tweet.objects.filter(tweet_id="3").exists()


@pytest.mark.django_db
def test_purge_old_fetch_runs(settings):
    settings.FETCH_RUN_RETENTION_DAYS = 90
    old = FetchRun.objects.create(run_id="old", subsystem="live", status="completed")
    FetchRun.objects.filter(pk=old.pk).update(
        started_at=timezone.now() - timedelta(days=91)
    )
    FetchRun.objects.create(run_id="new", subsystem="live", status="completed")
    FetchRun.objects.create(run_id="running", subsystem="live", status="running")
    FetchRun.objects.filter(run_id="running").update(
        started_at=timezone.now() - timedelta(days=91)
    )

    deleted = purge_old_fetch_runs()
    assert deleted == 1
    assert not FetchRun.objects.filter(run_id="old").exists()
    assert FetchRun.objects.filter(run_id="new").exists()
    assert FetchRun.objects.filter(run_id="running").exists()


@pytest.mark.django_db
def test_poll_live_all_runs_one_global_cycle(settings):
    settings.FETCH_LIVE_INTERVAL_SECONDS = 600
    TwitterUser.objects.create(handle="jack", tracking=True, priority=1)
    TwitterUser.objects.create(handle="elon", tracking=True, priority=2)
    with patch("apps.fetching.tasks._run_cycle", return_value=4) as run:
        assert poll_live_all() == 4
        run.assert_called_once()
        assert run.call_args.args[1] == ["--once"]
        assert run.call_args.args[2] == "live"


@pytest.mark.django_db
def test_poll_live_all_skips_overlapping_cycle(settings):
    settings.FETCH_LIVE_INTERVAL_SECONDS = 600
    with patch("apps.fetching.tasks._run_cycle", return_value=1) as run:
        assert poll_live_all() == 1
        assert poll_live_all() == 0
        run.assert_called_once()


@pytest.mark.django_db
def test_backfill_and_repoll_run_one_cycle(settings):
    settings.FETCH_HISTORICAL_INTERVAL_SECONDS = 21600
    settings.FETCH_SEARCH_INTERVAL_SECONDS = 1800
    TwitterUser.objects.create(handle="jack", tracking=True)
    search = Search.objects.create(name="ai", slug="ai", raw_query="ai", enabled=True)
    with patch("apps.fetching.tasks._run_cycle", return_value=1) as run:
        assert backfill_historical_all() == 1
        assert repoll_searches() == 1
        assert run.call_count == 2
        assert run.call_args_list[0].args[2] == "historical"
        assert run.call_args_list[1].args[2] == "search"
        assert run.call_args_list[1].kwargs["searches"] == [search]
