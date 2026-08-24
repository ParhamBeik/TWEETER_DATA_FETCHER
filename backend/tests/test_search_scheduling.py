"""Per-search scheduling, and the early stop that makes a repoll cheap.

Integration level for the dispatcher (the due/not-due decision is a query over
Search rows and is the whole behaviour), unit level for the stop predicate.

What these pin down: three searches used to run back-to-back inside one
subprocess under one wall-clock timeout. Deep pages come from browser scrolling,
minutes per query, so the budget ran out partway through and the last query was
SIGKILLed on every cycle -- in production it never completed once.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from fetcher.search import SearchTimelineMonitor
from fetching.tasks import dispatch_due_searches, run_search
from tweets.models import Search


@pytest.fixture(autouse=True)
def _clear_cycle_locks():
    cache.clear()
    yield
    cache.clear()


def _search(slug, **kwargs):
    return Search.objects.create(
        name=slug, slug=slug, raw_query=slug, enabled=True, **kwargs
    )


@pytest.mark.django_db
def test_every_search_is_dispatched_as_its_own_task():
    """One task each, so no query can be starved by the ones ahead of it."""
    for slug in ("iran", "war", "brent"):
        _search(slug)

    with patch("fetching.tasks.run_search.delay") as delay:
        assert dispatch_due_searches() == 3

    assert delay.call_count == 3


@pytest.mark.django_db
def test_a_search_that_ran_recently_is_not_due():
    _search("iran", interval_seconds=1800, last_run_at=timezone.now() - timedelta(minutes=5))

    with patch("fetching.tasks.run_search.delay") as delay:
        assert dispatch_due_searches() == 0

    delay.assert_not_called()


@pytest.mark.django_db
def test_each_search_keeps_its_own_cadence():
    _search("fast", interval_seconds=300, last_run_at=timezone.now() - timedelta(minutes=10))
    _search("slow", interval_seconds=86400, last_run_at=timezone.now() - timedelta(minutes=10))

    with patch("fetching.tasks.run_search.delay") as delay:
        assert dispatch_due_searches() == 1

    assert Search.objects.get(id=delay.call_args.args[0]).slug == "fast"


@pytest.mark.django_db
def test_a_search_that_never_ran_is_due_immediately():
    _search("iran", last_run_at=None)

    with patch("fetching.tasks.run_search.delay") as delay:
        assert dispatch_due_searches() == 1

    delay.assert_called_once()


@pytest.mark.django_db
def test_disabled_searches_are_never_dispatched():
    Search.objects.create(name="off", slug="off", raw_query="off", enabled=False)

    with patch("fetching.tasks.run_search.delay") as delay:
        assert dispatch_due_searches() == 0

    delay.assert_not_called()


@pytest.mark.django_db
def test_a_partial_run_still_waits_its_interval_before_retrying():
    """Otherwise the query least able to finish is retried every dispatcher tick."""
    search = _search("brent", interval_seconds=1800)

    def partial_cycle(*_args, **_kwargs):
        search.last_run_at = timezone.now()
        search.save(update_fields=["last_run_at"])
        return 5

    with patch("fetching.tasks._run_cycle", side_effect=partial_cycle):
        assert run_search(search.id) == 5

    with patch("fetching.tasks.run_search.delay") as delay:
        assert dispatch_due_searches() == 0
    delay.assert_not_called()


@pytest.mark.django_db
def test_run_search_collapses_an_overlapping_trigger():
    """Two browser bootstraps against the one shared X session must not overlap."""
    search = _search("iran")

    def nested(*_args, **_kwargs):
        assert run_search(search.id) == 0
        return 1

    with patch("fetching.tasks._run_cycle", side_effect=nested) as cycle:
        assert run_search(search.id) == 1
    cycle.assert_called_once()


# --- Early stop -------------------------------------------------------------


def _monitor():
    monitor = SearchTimelineMonitor.__new__(SearchTimelineMonitor)
    monitor._parse_search_page = lambda payload, seen, capture_debug=False: {
        "tweets": payload["tweets"]
    }
    return monitor


def _at(hours_ago):
    return datetime.utcnow() - timedelta(hours=hours_ago)


class _Page(dict):
    """A payload whose parse result the stub returns verbatim."""


def test_scrolling_stops_once_a_page_is_entirely_older_than_last_run(monkeypatch):
    monitor = _monitor()
    monkeypatch.setattr(
        "fetcher.search.validate_graphql_payload", lambda *_: MagicMock(ok=True)
    )
    monitor._tweet_datetime = lambda tweet: tweet["at"]
    monitor._page_crossed_search_window = SearchTimelineMonitor._page_crossed_search_window.__get__(monitor)

    stop = monitor._deep_stop_predicate(window_start=_at(240), known_ground=_at(6))

    assert stop(_Page(tweets=[{"at": _at(1)}, {"at": _at(2)}])) is False
    assert stop(_Page(tweets=[{"at": _at(7)}, {"at": _at(9)}])) is True


def test_scrolling_stops_at_the_rolling_window_even_on_a_first_run(monkeypatch):
    monitor = _monitor()
    monkeypatch.setattr(
        "fetcher.search.validate_graphql_payload", lambda *_: MagicMock(ok=True)
    )
    monitor._tweet_datetime = lambda tweet: tweet["at"]
    monitor._page_crossed_search_window = SearchTimelineMonitor._page_crossed_search_window.__get__(monitor)

    stop = monitor._deep_stop_predicate(window_start=_at(24), known_ground=None)

    assert stop(_Page(tweets=[{"at": _at(2)}])) is False
    assert stop(_Page(tweets=[{"at": _at(30)}])) is True


def test_an_unparseable_page_never_stops_the_scroll(monkeypatch):
    monitor = _monitor()
    monkeypatch.setattr(
        "fetcher.search.validate_graphql_payload", lambda *_: MagicMock(ok=False)
    )

    stop = monitor._deep_stop_predicate(window_start=_at(24), known_ground=_at(1))

    assert stop(_Page(tweets=[])) is False


@pytest.mark.parametrize(
    "state, expected",
    [
        ({}, None),
        ({"newest_seen_at": "not a date"}, None),
        ({"newest_seen_at": "2026-08-24T12:00:00Z"}, datetime(2026, 8, 24, 12, 0, 0)),
    ],
)
def test_known_ground_survives_a_missing_or_broken_state_blob(state, expected):
    assert SearchTimelineMonitor._parse_known_ground(state) == expected


def test_reaching_known_ground_is_a_success_the_run_can_record(monkeypatch):
    """The optimization has to be able to advance the mark it stops at.

    The browser reports stop_reason="predicate" for both stop conditions, so if
    the page loop only recognised the window crossing, a known-ground stop fell
    through to "partial_browser_predicate" -- and a partial run may not advance
    newest_seen_at. The fast path could never move its own high-water mark.
    """
    from fetcher.search import SearchTimelineMonitor

    monitor = SearchTimelineMonitor.__new__(SearchTimelineMonitor)
    monitor._tweet_datetime = lambda tweet: tweet["at"]
    monitor._page_crossed_search_window = (
        SearchTimelineMonitor._page_crossed_search_window.__get__(monitor)
    )

    stop, reason = monitor.should_stop_search_pagination(
        page_result={"tweets": [{"at": _at(7)}], "next_cursor": "c2"},
        window_start=_at(240),
        cursor="c1",
        cursor_history=set(),
        known_ground=_at(6),
    )

    assert stop is True
    assert reason == "success_reached_known_ground"


def test_the_known_ground_reason_counts_as_a_completed_run():
    import inspect

    from fetcher.search import SearchTimelineMonitor

    source = inspect.getsource(SearchTimelineMonitor.monitor_search)
    successful = source.split("successful_reasons = {")[1].split("}")[0]
    assert "success_reached_known_ground" in successful


def test_fresh_pages_do_not_trip_the_known_ground_stop(monkeypatch):
    from fetcher.search import SearchTimelineMonitor

    monitor = SearchTimelineMonitor.__new__(SearchTimelineMonitor)
    monitor._tweet_datetime = lambda tweet: tweet["at"]
    monitor._page_crossed_search_window = (
        SearchTimelineMonitor._page_crossed_search_window.__get__(monitor)
    )

    stop, reason = monitor.should_stop_search_pagination(
        page_result={"tweets": [{"at": _at(1)}], "next_cursor": "c2"},
        window_start=_at(240),
        cursor="c1",
        cursor_history=set(),
        known_ground=_at(6),
    )

    assert stop is False
    assert reason is None
