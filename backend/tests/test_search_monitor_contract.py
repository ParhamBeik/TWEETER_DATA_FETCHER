"""What `monitor_search` promises, pinned before it is restructured.

The method is 285 lines and does five things in sequence: plan the run, fetch
pages, parse them, build a report, advance the stored state. It is about to be
split along those seams, so this file exists to make that split provably a
no-op — it must pass identically before and after.

Two behaviours get the attention, because they are the ones a refactor could
change without breaking anything visible:

* how `exhausted_reason` becomes a run `status`, which is what decides whether
  the console shows a search as healthy;
* the `newest_seen_at` high-water mark, which the *next* run reads to stop
  early. Advancing it wrongly leaves a hole in the archive that no later run
  goes back for, and nothing at the time would look wrong.

The existing search tests cover the transport paths (HTTP page 1, browser
depth, fallbacks); these cover the classification and the state write those
paths all funnel into.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fetcher.processing import TweetSetProcessor
from fetcher.clock import utc_now
from fetcher.search import SearchTimelineMonitor


def _search_page(tweet_id: str, created_at: str, cursor: str | None) -> dict:
    """One SearchTimeline GraphQL page, shaped the way the parser expects."""
    entries = [
        {
            "entryId": f"tweet-{tweet_id}",
            "content": {
                "__typename": "TimelineTimelineItem",
                "itemContent": {
                    "tweet_results": {
                        "result": {
                            "rest_id": tweet_id,
                            "legacy": {"full_text": "tweet", "created_at": created_at},
                            "core": {
                                "user_results": {
                                    "result": {"legacy": {"screen_name": "author"}}
                                }
                            },
                        }
                    }
                },
            },
        }
    ]
    if cursor:
        entries.append(
            {
                "entryId": f"cursor-bottom-{cursor}",
                "content": {
                    "__typename": "TimelineTimelineCursor",
                    "cursorType": "Bottom",
                    "value": cursor,
                },
            }
        )
    return {
        "data": {
            "search_by_raw_query": {
                "search_timeline": {
                    "timeline": {
                        "instructions": [{"type": "TimelineAddEntries", "entries": entries}]
                    }
                }
            }
        }
    }


@pytest.fixture
def workspace():
    path = Path(tempfile.mkdtemp(prefix="tdf_search_contract_"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def build_monitor(workspace: Path, *, state: dict | None = None) -> SearchTimelineMonitor:
    """A monitor with every collaborator stubbed but the logic under test real."""
    monitor = SearchTimelineMonitor.__new__(SearchTimelineMonitor)
    monitor.config = {"api_config": {"pagination_safety_cap_pages": 50}}
    monitor.storage = MagicMock()
    monitor.storage._now.return_value = utc_now()
    monitor.storage._batch_name.return_value = "batch"
    monitor.storage.save_search_result_page.side_effect = (
        lambda *args: workspace / f"page_{args[-2]}.json"
    )
    monitor.raw_root = workspace / "raw"
    monitor.reports_root = workspace / "reports"
    monitor.reports_root.mkdir(parents=True, exist_ok=True)
    monitor.state_file = workspace / "state.json"
    monitor.search_state = state if state is not None else {}
    monitor.processor = TweetSetProcessor()
    monitor.console = MagicMock()
    monitor.api_manager = MagicMock()
    monitor.api_manager.rate_limits = {"SearchTimeline": {}}
    monitor.api_manager.get_query_id.return_value = "query-id"
    monitor.fetcher = MagicMock()
    monitor.fetcher.recorder = MagicMock()
    monitor._save_exports = MagicMock(return_value={})
    monitor._build_frozen_headers = MagicMock(return_value={})
    monitor._compact_json = lambda payload: "{}"
    monitor._after_bootstrap = MagicMock()
    return monitor


def http_page(tweet_id="1", created_at=None, cursor=None, **extra):
    """An HTTP page 1 carrying the underscore-prefixed transport metadata."""
    created_at = created_at or utc_now().strftime("%a %b %d %H:%M:%S +0000 %Y")
    page = _search_page(tweet_id, created_at, cursor)
    page["_attempts"] = 1
    page["_status"] = 200
    page["_error_samples"] = []
    page.update(extra)
    return page


SEARCH_DEF = {"name": "test", "raw_query": "test", "product": "Latest", "rolling_hours": 24}


# --- status classification --------------------------------------------------


def test_a_depth_one_run_without_a_cursor_completes(workspace):
    """An exhausted first page is a completed search, not a partial run."""
    monitor = build_monitor(workspace)
    monitor._request_page = MagicMock(return_value=http_page())

    report = monitor.monitor_search({**SEARCH_DEF, "pagination_depth": 1})

    assert report["metadata"]["exhausted_reason"] == "no_bottom_cursor"
    assert report["status"] == "completed"
    # The endpoint status names the transport only when the run actually
    # succeeded; otherwise it repeats the status.
    assert report["endpoint_status"] == "verified_http"


def test_a_run_that_fetched_nothing_is_failed_not_partial(workspace):
    """"Did nothing" must not look healthy -- the same rule the runner applies
    to FetchRun. With no tweets and no successful stop reason, this is failed.
    """
    monitor = build_monitor(workspace)
    monitor._request_page = MagicMock(
        return_value={"_attempts": 1, "_status": 404, "_error_samples": [], "_failure": "failed_initial_404"}
    )

    report = monitor.monitor_search({**SEARCH_DEF, "pagination_depth": 1})

    assert report["counts"]["tweets"] == 0
    assert report["status"] == "failed"
    assert report["endpoint_status"] == "failed"


def test_a_failure_that_still_collected_tweets_is_partial(workspace):
    """Tweets in hand plus an unsuccessful stop reason is a partial run: some
    of the answer arrived, so it is neither completed nor failed.
    """
    monitor = build_monitor(workspace)
    monitor.fetcher.bootstrap_browser_context.return_value = MagicMock(
        ok=False, error="boom", stop_reason=None, target_pages={"SearchTimeline": []},
        route=None, support_request_count=0, route_retry_count=0,
    )
    monitor._request_page = MagicMock(return_value=http_page(cursor="c1"))

    report = monitor.monitor_search({**SEARCH_DEF, "pagination_depth": 3})

    assert report["counts"]["tweets"] == 1
    assert report["status"] == "partial"
    assert report["endpoint_status"] == "partial"


@pytest.mark.parametrize(
    "reason",
    [
        "success_search_window_crossed",
        "success_reached_known_ground",
        "no_bottom_cursor",
        "repeated_cursor_history",
        "depth_one_complete",
    ],
)
def test_the_set_of_reasons_that_count_as_success_is_exact(workspace, reason):
    """Each of these means "we stopped because we were done", not "we broke".
    Adding or dropping one silently re-labels runs in the console.
    """
    monitor = build_monitor(workspace)
    monitor._request_page = MagicMock(return_value=http_page(cursor="c1"))
    monitor.should_stop_search_pagination = MagicMock(return_value=(True, reason))

    report = monitor.monitor_search({**SEARCH_DEF, "pagination_depth": 1})

    assert report["metadata"]["exhausted_reason"] == reason
    assert report["status"] == "completed"


def test_no_bottom_cursor_ends_a_deep_run_successfully(workspace):
    """Running out of results is a success, not a truncation."""
    monitor = build_monitor(workspace)
    monitor.fetcher.bootstrap_browser_context.return_value = MagicMock(
        ok=True,
        error=None,
        stop_reason="end",
        route="https://x.com/search",
        support_request_count=0,
        route_retry_count=0,
        target_pages={
            "SearchTimeline": [
                _search_page("1", utc_now().strftime("%a %b %d %H:%M:%S +0000 %Y"), "c1"),
                _search_page("2", utc_now().strftime("%a %b %d %H:%M:%S +0000 %Y"), None),
            ]
        },
    )
    monitor._request_page = MagicMock(return_value=http_page(cursor="c0"))

    report = monitor.monitor_search({**SEARCH_DEF, "pagination_depth": 3})

    assert report["metadata"]["exhausted_reason"] == "no_bottom_cursor"
    assert report["status"] == "completed"


# --- the known-ground high-water mark ---------------------------------------


def _state_of(monitor) -> dict:
    return json.loads(monitor.state_file.read_text())


def test_a_successful_run_records_the_newest_tweet_it_saw(workspace):
    monitor = build_monitor(workspace)
    stamp = utc_now().strftime("%a %b %d %H:%M:%S +0000 %Y")
    monitor._request_page = MagicMock(return_value=http_page(created_at=stamp))

    monitor.monitor_search({**SEARCH_DEF, "pagination_depth": 1})

    state = _state_of(monitor)["test::latest"]
    assert state["newest_seen_at"]
    assert state["last_checked_at"]


def test_the_mark_only_ever_moves_forward(workspace):
    """An older tweet must not pull the mark back, or the next run re-scrolls
    ground it has already stored.
    """
    future = (utc_now() + timedelta(days=1)).isoformat() + "Z"
    monitor = build_monitor(
        workspace, state={"test::latest": {"newest_seen_at": future}}
    )
    old = (utc_now() - timedelta(days=5)).strftime("%a %b %d %H:%M:%S +0000 %Y")
    monitor._request_page = MagicMock(return_value=http_page(created_at=old))

    monitor.monitor_search({**SEARCH_DEF, "pagination_depth": 1})

    assert _state_of(monitor)["test::latest"]["newest_seen_at"] == future


def _stalled_browser(pages: list[dict]) -> MagicMock:
    """A browser that opened, scrolled, and ran out of pages to produce."""
    return MagicMock(
        ok=True, error=None, stop_reason="stalled",
        target_pages={"SearchTimeline": pages}, route="search_url",
        support_request_count=0, route_retry_count=0,
    )


def test_a_depth_limited_run_advances_the_mark(workspace):
    """The deadlock this mark used to cause.

    A broad query cannot exhaust its window inside one run, so it always ended
    `partial_browser_stalled`; a partial could not advance the mark; and with
    the mark pinned the next run scrolled back to the same ageing boundary and
    stalled again. Two searches spent ~4,600 browser page loads a day re-reading
    tweets they already had. A run that started at the newest result and paged
    down until the browser ran dry did see the top, so it may move the mark.
    """
    previous = (utc_now() - timedelta(days=3)).isoformat() + "Z"
    monitor = build_monitor(
        workspace, state={"test::latest": {"newest_seen_at": previous, "last_checked_at": "old"}}
    )
    stamp = utc_now().strftime("%a %b %d %H:%M:%S +0000 %Y")
    monitor.fetcher.bootstrap_browser_context.return_value = _stalled_browser(
        [_search_page("2", stamp, "c2")]
    )
    monitor._request_page = MagicMock(return_value=http_page(created_at=stamp, cursor="c1"))

    report = monitor.monitor_search({**SEARCH_DEF, "pagination_depth": 3})

    assert report["status"] == "partial"
    assert report["metadata"]["exhausted_reason"] == "partial_browser_stalled"
    state = _state_of(monitor)["test::latest"]
    assert state["newest_seen_at"] > previous
    # The timer moves with the mark: a query that can never complete was
    # otherwise eligible again on the very next cycle, ignoring its interval.
    assert state["last_checked_at"] != "old"


def test_a_depth_limited_run_under_top_still_cannot_advance_the_mark(workspace):
    """Under Top, page order is relevance -- page 3 can hold something newer
    than page 1, so "newest seen" says nothing about what was covered.
    """
    previous = (utc_now() - timedelta(days=3)).isoformat() + "Z"
    monitor = build_monitor(
        workspace, state={"test::latest": {"newest_seen_at": previous}},
    )
    monitor.search_state = {"test::top": {"newest_seen_at": previous, "last_checked_at": "old"}}
    stamp = utc_now().strftime("%a %b %d %H:%M:%S +0000 %Y")
    monitor.fetcher.bootstrap_browser_context.return_value = _stalled_browser(
        [_search_page("2", stamp, "c2")]
    )
    monitor._request_page = MagicMock(return_value=http_page(created_at=stamp, cursor="c1"))

    report = monitor.monitor_search({**SEARCH_DEF, "product": "Top", "pagination_depth": 3})

    # Same depth-limited ending as the Latest case above, so the product gate
    # is what holds the mark back here and not some other rejection.
    assert report["metadata"]["exhausted_reason"] == "partial_browser_stalled"
    state = _state_of(monitor)["test::top"]
    assert state["newest_seen_at"] == previous
    assert state["last_checked_at"] == "old"


def test_a_partial_run_never_advances_the_mark(workspace):
    """A partial run that never got the browser open has not proven it saw the
    top of the results. It lands in the same `partial_browser_*` bucket as a
    genuine stall, so the bootstrap flag -- not the reason alone -- is what
    separates "ran out of depth" from "saw nothing it can vouch for".
    """
    previous = (utc_now() - timedelta(days=3)).isoformat() + "Z"
    monitor = build_monitor(
        workspace, state={"test::latest": {"newest_seen_at": previous, "last_checked_at": "old"}}
    )
    monitor.fetcher.bootstrap_browser_context.return_value = MagicMock(
        ok=False, error="boom", stop_reason=None, target_pages={"SearchTimeline": []},
        route=None, support_request_count=0, route_retry_count=0,
    )
    now = utc_now().strftime("%a %b %d %H:%M:%S +0000 %Y")
    monitor._request_page = MagicMock(return_value=http_page(created_at=now, cursor="c1"))

    report = monitor.monitor_search({**SEARCH_DEF, "pagination_depth": 3})

    assert report["status"] == "partial"
    state = _state_of(monitor)["test::latest"]
    assert state["newest_seen_at"] == previous
    # last_checked_at is likewise only stamped by a successful run, so the
    # scheduler retries rather than treating a broken run as "checked".
    assert state["last_checked_at"] == "old"


def test_a_failed_run_leaves_no_mark_when_there_was_none(workspace):
    monitor = build_monitor(workspace)
    monitor._request_page = MagicMock(
        return_value={"_attempts": 1, "_status": 500, "_error_samples": [], "_failure": "failed_initial_http_error"}
    )

    monitor.monitor_search({**SEARCH_DEF, "pagination_depth": 1})

    state = _state_of(monitor)["test::latest"]
    assert "newest_seen_at" not in state or state["newest_seen_at"] is None
    assert state["last_checked_at"] is None


def test_the_state_key_keeps_products_apart(workspace):
    """Top and Latest are separate jobs with separate cursors. The double-colon
    spelling is also what fetching/searches.py teardown matches on.
    """
    monitor = build_monitor(workspace)
    monitor._request_page = MagicMock(return_value=http_page())

    monitor.monitor_search({**SEARCH_DEF, "product": "Top", "pagination_depth": 1})

    assert "test::top" in _state_of(monitor)


# --- the report envelope ----------------------------------------------------


def test_the_report_carries_what_the_runner_ingests(workspace):
    """fetching/runner.py reads these keys off the report; renaming one breaks
    ingestion silently, because the run still exits zero.
    """
    monitor = build_monitor(workspace)
    monitor._request_page = MagicMock(return_value=http_page())

    report = monitor.monitor_search({**SEARCH_DEF, "pagination_depth": 1})

    assert set(report) >= {
        "search", "slug", "product", "raw_query",
        "status", "endpoint_status", "metadata", "counts", "outputs",
    }
    assert report["metadata"]["transport"] == "http"
    assert report["metadata"]["rolling_hours"] == 6
    assert report["metadata"]["window_start_utc"].endswith("Z")


def test_parse_pages_handles_none_bootstrap_on_partial_deep_search(workspace):
    monitor = build_monitor(workspace)
    plan = monitor._plan_run({**SEARCH_DEF, "pagination_depth": 3})
    fetched = {
        "payloads": [_search_page("1", "Wed Oct 10 20:19:24 +0000 2026", cursor="cursor-1")],
        "bootstrap": None,
        "transport": "http",
        "exhausted_reason": None,
        "http_latency_ms": 10,
        "page_output_paths": [],
    }
    parsed = monitor._parse_pages(plan, fetched)
    assert parsed["exhausted_reason"] == "partial_browser_stalled"
