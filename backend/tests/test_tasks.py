"""Retention tasks: search-only TTL and FetchRun purge."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from fetching.ingest import ingest_search_hits, upsert_tweet
from fetching.tasks import (
    _backfill_queue,
    backfill_historical_all,
    poll_live_all,
    purge_expired_search_tweets,
    purge_old_fetch_runs,
    purge_old_raw_pages,
    repoll_searches,
)
from tweets.models import (
    EndpointState,
    FetchRun,
    RawPage,
    Search,
    SearchHit,
    SearchTweet,
    Tweet,
    TwitterUser,
)


@pytest.fixture(autouse=True)
def _clear_cycle_locks():
    cache.clear()
    yield
    cache.clear()


def _hit(rest_id: str, account: str, text: str) -> dict:
    return {
        "rest_id": rest_id,
        "author_id": f"9{rest_id}",
        "account": account,
        "text": text,
        "created_at": "Wed Oct 10 20:19:24 +0000 2018",
    }


@pytest.mark.django_db
def test_purge_expired_search_tweets_drops_only_stale_hits(settings):
    """Search retention runs on its own clock over its own table.

    The tracked-account archive used to share that table, so this task had to
    exclude tracked handles by hand to avoid deleting the archive. The split is
    what makes the rule simply "a stale search hit", and this asserts the archive
    is now out of reach rather than merely excluded.
    """
    settings.SEARCH_TWEET_TTL_DAYS = 30
    TwitterUser.objects.create(handle="jack", tracking=True)
    search = Search.objects.create(name="ai", slug="ai", raw_query="ai")

    ingest_search_hits(search, [_hit("1", "random", "old search"), _hit("3", "fresh", "fresh")])
    stale = timezone.now() - timedelta(days=31)
    SearchTweet.objects.filter(tweet_id="1").update(ingested_at=stale)
    SearchHit.objects.filter(search_tweet__tweet_id="1").update(last_seen_at=stale)

    archived = upsert_tweet(_hit("2", "jack", "old but tracked"))
    Tweet.objects.filter(pk=archived.pk).update(ingested_at=stale)

    deleted = purge_expired_search_tweets()

    assert deleted == 1
    assert not SearchTweet.objects.filter(tweet_id="1").exists()
    assert SearchTweet.objects.filter(tweet_id="3").exists()
    # The tracked archive is a different table entirely and cannot be caught.
    assert Tweet.objects.filter(tweet_id="2").exists()


@pytest.mark.django_db
def test_purge_expired_search_tweets_keeps_a_body_a_live_search_still_wants(settings):
    """A tweet two queries matched survives until neither wants it."""
    settings.SEARCH_TWEET_TTL_DAYS = 30
    stale_search = Search.objects.create(name="old", slug="old", raw_query="old")
    live_search = Search.objects.create(name="new", slug="new", raw_query="new")
    ingest_search_hits(stale_search, [_hit("7", "someone", "shared post")])
    ingest_search_hits(live_search, [_hit("7", "someone", "shared post")])

    stale = timezone.now() - timedelta(days=31)
    SearchTweet.objects.filter(tweet_id="7").update(ingested_at=stale)
    SearchHit.objects.filter(search=stale_search).update(last_seen_at=stale)

    assert purge_expired_search_tweets() == 0
    assert SearchTweet.objects.filter(tweet_id="7").exists()
    assert SearchHit.objects.filter(search=live_search).exists()
    assert not SearchHit.objects.filter(search=stale_search).exists()


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
    FetchRun.objects.create(run_id="fresh-running", subsystem="live", status="running")

    deleted = purge_old_fetch_runs()
    assert deleted == 2
    assert not FetchRun.objects.filter(run_id="old").exists()
    assert not FetchRun.objects.filter(run_id="running").exists()
    assert FetchRun.objects.filter(run_id="new").exists()
    assert FetchRun.objects.filter(run_id="fresh-running").exists()


@pytest.mark.django_db
def test_purge_old_raw_pages_deletes_only_expired_rows(settings):
    """Unit on the age cutoff: this is the retention clock, not the run FK."""
    settings.RAW_PAGE_RETENTION_DAYS = 30
    old = RawPage.objects.create(
        endpoint="UserTweets", account="jack", batch="b", page_number=1, payload={}
    )
    RawPage.objects.filter(pk=old.pk).update(
        created_at=timezone.now() - timedelta(days=31)
    )
    RawPage.objects.create(
        endpoint="UserTweets", account="jack", batch="b", page_number=2, payload={}
    )

    assert purge_old_raw_pages() == 1
    assert not RawPage.objects.filter(page_number=1).exists()
    assert RawPage.objects.filter(page_number=2).exists()


@pytest.mark.django_db
def test_poll_live_all_runs_one_global_cycle(settings):
    settings.FETCH_LIVE_INTERVAL_SECONDS = 600
    TwitterUser.objects.create(handle="jack", tracking=True, priority=1)
    TwitterUser.objects.create(handle="elon", tracking=True, priority=2)
    with patch("fetching.tasks._run_cycle", return_value=4) as run:
        assert poll_live_all() == 4
        run.assert_called_once()
        assert run.call_args.args[1] == ["--once"]
        assert run.call_args.args[2] == "live"


@pytest.mark.django_db
def test_poll_live_all_skips_overlapping_cycle(settings):
    settings.FETCH_LIVE_INTERVAL_SECONDS = 600

    def nested(_module, _args, _subsystem):
        assert poll_live_all() == 0
        return 1

    with patch("fetching.tasks._run_cycle", side_effect=nested) as run:
        assert poll_live_all() == 1
        run.assert_called_once()


@pytest.mark.django_db
def test_backfill_and_repoll_run_one_cycle(settings):
    settings.FETCH_HISTORICAL_INTERVAL_SECONDS = 21600
    settings.FETCH_SEARCH_INTERVAL_SECONDS = 1800
    TwitterUser.objects.create(handle="jack", tracking=True)
    search = Search.objects.create(name="ai", slug="ai", raw_query="ai", enabled=True)
    with patch("fetching.tasks._run_cycle", return_value=1) as run:
        assert backfill_historical_all() == 1
        assert repoll_searches() == 1
        assert run.call_count == 2
        assert run.call_args_list[0].args[2] == "historical"
        assert run.call_args_list[1].args[2] == "search"
        assert run.call_args_list[1].kwargs["searches"] == [search]


# --- Archive backfill queue -------------------------------------------------
#
# Integration level (real DB, real queue function): the behaviour under test is
# a join between TwitterUser rows and the engine's EndpointState blobs, and the
# lowercasing mismatch between those two stores is exactly what a mocked version
# would paper over.


def _archive(handle: str, **data):
    EndpointState.objects.create(account=handle.lower(), endpoint="UserTweets", data=data)


@pytest.mark.django_db
def test_backfill_queue_drops_accounts_whose_archive_is_complete():
    TwitterUser.objects.create(handle="Business", tracking=True, priority=1)
    TwitterUser.objects.create(handle="Reuters", tracking=True, priority=2)
    _archive("Business", backfill_complete=True, backfill_pages_done=43)

    assert _backfill_queue(10) == ["Reuters"]


@pytest.mark.django_db
def test_backfill_queue_sinks_an_account_that_cannot_make_progress():
    """The starvation bug: one stuck account must not block everyone behind it."""
    TwitterUser.objects.create(handle="Stuck", tracking=True, priority=1)
    TwitterUser.objects.create(handle="Healthy", tracking=True, priority=7)
    _archive("Stuck", backfill_stalled_ticks=3)

    assert _backfill_queue(10) == ["Healthy", "Stuck"]


@pytest.mark.django_db
def test_backfill_queue_prefers_higher_tier_when_all_are_progressing():
    TwitterUser.objects.create(handle="Low", tracking=True, priority=7)
    TwitterUser.objects.create(handle="High", tracking=True, priority=1)

    assert _backfill_queue(1) == ["High"]


@pytest.mark.django_db
def test_backfill_queue_skips_quarantined_accounts():
    TwitterUser.objects.create(handle="Ghost", tracking=True, priority=1, quarantined=True)
    TwitterUser.objects.create(handle="Real", tracking=True, priority=7)

    assert _backfill_queue(10) == ["Real"]


@pytest.mark.django_db
def test_backfill_stops_dispatching_once_every_account_is_archived():
    TwitterUser.objects.create(handle="Business", tracking=True)
    _archive("Business", backfill_complete=True)

    with patch("fetching.tasks._run_cycle") as run:
        assert backfill_historical_all() == 0
        run.assert_not_called()
