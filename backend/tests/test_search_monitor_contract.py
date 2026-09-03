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
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fetcher.processing import TweetSetProcessor
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
    monitor.storage._now.return_value = datetime.utcnow()
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
    created_at = created_at or datetime.utcnow().strftime("%a %b %d %H:%M:%S +0000 %Y")
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
                _search_page("1", datetime.utcnow().strftime("%a %b %d %H:%M:%S +0000 %Y"), "c1"),
                _search_page("2", datetime.utcnow().strftime("%a %b %d %H:%M:%S +0000 %Y"), None),
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
    stamp = datetime.utcnow().strftime("%a %b %d %H:%M:%S +0000 %Y")
    monitor._request_page = MagicMock(return_value=http_page(created_at=stamp))

    monitor.monitor_search({**SEARCH_DEF, "pagination_depth": 1})

    state = _state_of(monitor)["test::latest"]
    assert state["newest_seen_at"]
    assert state["last_checked_at"]


def test_the_mark_only_ever_moves_forward(workspace):
    """An older tweet must not pull the mark back, or the next run re-scrolls
    ground it has already stored.
    """
    future = (datetime.utcnow() + timedelta(days=1)).isoformat() + "Z"
    monitor = build_monitor(
        workspace, state={"test::latest": {"newest_seen_at": future}}
    )
    old = (datetime.utcnow() - timedelta(days=5)).strftime("%a %b %d %H:%M:%S +0000 %Y")
    monitor._request_page = MagicMock(return_value=http_page(created_at=old))

    monitor.monitor_search({**SEARCH_DEF, "pagination_depth": 1})

    assert _state_of(monitor)["test::latest"]["newest_seen_at"] == future


def test_a_partial_run_never_advances_the_mark(workspace):
    """A partial run has not proven it saw the top of the results. Trusting its
    newest tweet would leave a hole no later run goes back for -- and nothing
    at the time would look wrong.
    """
    previous = (datetime.utcnow() - timedelta(days=3)).isoformat() + "Z"
    monitor = build_monitor(
        workspace, state={"test::latest": {"newest_seen_at": previous, "last_checked_at": "old"}}
    )
    monitor.fetcher.bootstrap_browser_context.return_value = MagicMock(
        ok=False, error="boom", stop_reason=None, target_pages={"SearchTimeline": []},
        route=None, support_request_count=0, route_retry_count=0,
    )
    now = datetime.utcnow().strftime("%a %b %d %H:%M:%S +0000 %Y")
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
    assert report["metadata"]["rolling_hours"] == 24
    assert report["metadata"]["window_start_utc"].endswith("Z")
