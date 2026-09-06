"""The archive walk: when it stops, and where it resumes.

Unit level with a stubbed HTTP layer, because what is under test is the
pagination loop's termination and cursor bookkeeping -- not the network, not the
database. Each case drives the real ``_fetch_endpoint_result`` against a scripted
sequence of GraphQL pages.

Production history these pin down: one account's timeline ran out of tweets but
X kept handing out bottom cursors, so the walk paginated into the void until its
safety cap, reported "partial", never advanced, and refetched the same account
from page 1 every 15 minutes -- draining the shared UserTweets budget and
deferring 100% of live polling for four days.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from fetcher.client import APIManager
from fetcher.timeline import EMPTY_PAGE_STREAK, FetcherEngine


def _tweet_entry(tweet_id: str) -> dict:
    return {
        "entryId": f"tweet-{tweet_id}",
        "content": {
            "itemContent": {
                "tweet_results": {
                    "result": {
                        "rest_id": tweet_id,
                        "legacy": {
                            "full_text": f"tweet {tweet_id}",
                            "created_at": "Mon Aug 24 12:00:00 +0000 2026",
                        },
                    }
                }
            }
        },
    }


def _page(*, tweets: int, cursor: str | None, offset: int = 0) -> dict:
    """One UserTweets page. ``cursor=None`` means the true end of pagination."""
    entries = [_tweet_entry(str(offset + index)) for index in range(tweets)]
    if cursor:
        entries.append(
            {"entryId": "cursor-bottom-0", "content": {"value": cursor, "cursorType": "Bottom"}}
        )
    return {
        "data": {
            "user": {
                "result": {
                    "timeline": {
                        "timeline": {
                            "instructions": [{"type": "TimelineAddEntries", "entries": entries}]
                        }
                    }
                }
            }
        }
    }


def _response(payload: dict):
    return SimpleNamespace(
        status_code=200,
        text=json.dumps(payload),
        json=lambda: payload,
        headers={},
        elapsed_seconds=0.01,
        request=SimpleNamespace(headers={}),
        raise_for_status=lambda: None,
    )


class ArchiveWalkTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        config_path = Path(self.temp_dir) / "config.json"
        config_path.write_text(json.dumps({"real_transaction_ids_by_endpoint": {}}))
        self.engine = FetcherEngine(config_path=str(config_path), subsystem="test")
        self.engine.logger = MagicMock()
        self.engine.recorder = MagicMock()
        self.engine.api_manager = MagicMock()
        self.engine.api_manager.get_query_id = MagicMock(return_value="qid")
        self.engine.api_manager.retry_policy = MagicMock(return_value={})
        self.engine.api_manager.rate_limits = {"UserTweets": {"limit": 50, "remaining": 50, "reset": 0}}
        self.engine.api_manager.remaining_requests = (
            lambda endpoint, reserve=0: APIManager.remaining_requests(
                self.engine.api_manager, endpoint, reserve
            )
        )

        # Real state manager so cursors round-trip exactly as they do in production.
        self.state: dict = {}
        storage = MagicMock()
        storage.get_endpoint_state = lambda account, endpoint: {
            "last_cursor": None, "status": "pending", **self.state
        }
        storage.update_endpoint_state = lambda account, endpoint, last_cursor=None, status=None, meta=None: (
            self.state.update(
                {k: v for k, v in {"last_cursor": last_cursor, "status": status}.items() if v is not None}
            ),
            self.state.update(meta or {}),
        )
        batch = Path(self.temp_dir) / "batch"
        batch.mkdir(parents=True, exist_ok=True)
        storage.create_raw_batch_dir.return_value = batch
        storage.load_raw_pages_from_batch.return_value = []
        storage.save_raw_page.return_value = batch / "page.json"
        self.engine.storage_manager = storage

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _serve(self, pages: list[dict]) -> MagicMock:
        get = MagicMock(side_effect=[_response(page) for page in pages])
        self.engine.api_manager.perform_get = get
        return get

    def _walk(self, **kwargs) -> dict:
        defaults = dict(
            account="business",
            user_id="34713362",
            endpoint="UserTweets",
            max_pages=25,
            window_days=None,
            cutoff=None,
            force_refetch=True,
        )
        return self.engine._fetch_endpoint_result(**{**defaults, **kwargs})

    def test_tweetless_pages_end_the_walk_instead_of_burning_the_page_budget(self):
        """The @business bug: pages past the last tweet still carry a cursor."""
        get = self._serve(
            [_page(tweets=20, cursor="c1", offset=0), _page(tweets=20, cursor="c2", offset=20)]
            + [_page(tweets=0, cursor=f"c{n}") for n in range(3, 3 + EMPTY_PAGE_STREAK)]
        )

        result = self._walk()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcome"], "success_timeline_exhausted")
        self.assertEqual(result["last_cursor"], "__END__")
        # Two real pages plus exactly the empty streak -- not the 25-page budget.
        self.assertEqual(get.call_count, 2 + EMPTY_PAGE_STREAK)

    def test_a_single_tweetless_page_does_not_end_the_walk(self):
        get = self._serve([
            _page(tweets=20, cursor="c1", offset=0),
            _page(tweets=0, cursor="c2"),
            _page(tweets=20, cursor="c3", offset=20),
            _page(tweets=20, cursor=None, offset=40),
        ])

        result = self._walk()

        self.assertEqual(result["outcome"], "success_true_end")
        self.assertEqual(get.call_count, 4)

    def test_page_budget_stops_the_tick_and_keeps_the_cursor_for_the_next_one(self):
        self._serve([_page(tweets=20, cursor=f"c{n}", offset=n * 20) for n in range(1, 4)])

        result = self._walk(max_pages=3)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["outcome"], "partial_safety_cap_reached")
        # The cursor the next tick must resume from, not "__START__".
        self.assertEqual(result["last_cursor"], "c3")

    def test_walk_resumes_from_the_supplied_cursor_rather_than_page_one(self):
        get = self._serve([_page(tweets=20, cursor=None, offset=60)])

        result = self._walk(resume_cursor="c3", force_refetch=False)

        self.assertEqual(result["status"], "completed")
        sent_url = get.call_args.kwargs["url"]
        self.assertIn("c3", sent_url)

    def test_resumed_tick_does_not_claim_the_live_watermark(self):
        """A tick that started deep in the past never saw the top of the timeline."""
        self._serve([_page(tweets=20, cursor=None, offset=60)])

        self._walk(resume_cursor="c3", force_refetch=False)

        self.assertNotIn("fetch_watermark", self.state)

    def test_fresh_walk_completing_does_advance_the_live_watermark(self):
        self._serve([_page(tweets=20, cursor=None, offset=0)])

        self._walk()

        self.assertIn("fetch_watermark", self.state)

    def test_walk_pauses_at_the_quota_floor_reserved_for_live_polling(self):
        self.engine.api_manager.rate_limits = {
            "UserTweets": {"limit": 50, "remaining": 20, "reset": 0}
        }
        get = self._serve([_page(tweets=20, cursor="c1", offset=0)])

        result = self._walk(min_remaining=20)

        self.assertEqual(result["outcome"], "paused_for_quota")
        get.assert_not_called()

    def test_walk_proceeds_while_above_the_quota_floor(self):
        self.engine.api_manager.rate_limits = {
            "UserTweets": {"limit": 50, "remaining": 21, "reset": 0}
        }
        self._serve([_page(tweets=20, cursor=None, offset=0)])

        result = self._walk(min_remaining=20)

        self.assertEqual(result["status"], "completed")

    def test_an_exhausted_walk_exposes_the_wall_cursor_for_a_later_probe(self):
        """last_cursor stays __END__ (live shares that field). The last real
        bottom cursor X still offered has to travel separately or the monthly
        probe has nothing to resume from."""
        self._serve(
            [_page(tweets=20, cursor="c1", offset=0)]
            + [_page(tweets=0, cursor=f"empty{n}") for n in range(EMPTY_PAGE_STREAK)]
        )

        result = self._walk()

        self.assertEqual(result["outcome"], "success_timeline_exhausted")
        self.assertEqual(result["last_cursor"], "__END__")
        self.assertEqual(result["bottom_cursor"], f"empty{EMPTY_PAGE_STREAK - 1}")


if __name__ == "__main__":
    unittest.main()


class ArchiveCompletionTests(unittest.TestCase):
    """Which outcomes may end an account's archive walk for good.

    Unit level on the bookkeeping function, because the distinction being
    guarded is a pure mapping from an outcome to a decision -- and getting it
    wrong is silent: the account simply never gets collected again.
    """

    def _record(self, outcome, *, status="completed", pages=5, previous=None, cutoff=None,
                last_cursor="c9", bottom_cursor=None):
        from fetcher.historical import _record_backfill_progress

        saved = {}
        storage = MagicMock()
        storage.update_endpoint_state = lambda account, endpoint, meta=None, **_: saved.update(meta or {})
        payload = {
            "status": status, "outcome": outcome, "pages_fetched": pages, "last_cursor": last_cursor,
        }
        if bottom_cursor is not None:
            payload["bottom_cursor"] = bottom_cursor
        _record_backfill_progress(
            storage, "business", "UserTweets", previous or {}, payload, cutoff=cutoff,
        )
        return saved

    def test_crossing_the_archive_date_floor_ends_the_walk(self):
        """The archive walk passes the floor as its only cutoff.

        Coverage can therefore only come from that floor -- timeline.py takes
        the cutoff branch and never looks at window_days -- so here this outcome
        unambiguously means "reached the date we chose to stop at", not "covered
        a tier's rolling few days". With no floor configured, no cutoff and no
        window_days means coverage is None and this outcome cannot fire at all.
        """
        saved = self._record("success_window_complete")

        self.assertIs(saved["backfill_complete"], True)
        self.assertEqual(saved["backfill_depth_reason"], "reached_date_floor")
        self.assertIsNone(saved["backfill_cursor"])

    def test_running_out_of_tweets_is_recorded_as_the_providers_limit(self):
        """X offering a cursor but no tweets is its serving depth, not the
        account's first tweet. It fires after two empty pages, and calling it
        completeness is what put 45 of 64 accounts at exactly 45 pages and let
        @elonmusk be reported fully archived holding three months of history.
        The walk still stops -- retrying cannot beat a provider limit -- but the
        reason has to survive, or the gap is invisible."""
        saved = self._record(
            "success_timeline_exhausted", last_cursor="__END__", bottom_cursor="c9",
        )

        self.assertIs(saved["backfill_complete"], True)
        self.assertEqual(saved["backfill_depth_reason"], "provider_depth_limit")
        self.assertEqual(saved["backfill_cursor"], "c9")

    def test_reaching_the_true_end_of_pagination_ends_the_walk(self):
        saved = self._record("success_true_end")

        self.assertIs(saved["backfill_complete"], True)
        self.assertEqual(saved["backfill_depth_reason"], "reached_first_tweet")

    def test_pausing_for_quota_is_not_a_stall(self):
        """It is the design working. Counting it demotes healthy accounts that
        happen to sit late in a chunk that ran the shared bucket down."""
        saved = self._record(
            "paused_for_quota", status="partial", pages=0, previous={"backfill_stalled_ticks": 2},
        )

        self.assertEqual(saved["backfill_stalled_ticks"], 2)
        self.assertNotIn("backfill_complete", saved)

    def test_a_non_terminal_tick_never_un_completes_a_finished_archive(self):
        """The over-correction to guard against.

        fetch_account_historical is reachable from the API and does not check
        completeness, so one failed manual refetch of an account that reached
        its first tweet must not flip it back to incomplete -- that drops it
        into the backfill queue for a full re-walk it will never need. A single
        tick carries no evidence about completeness either way, so it may set
        the flag but never clear it.
        """
        saved = self._record(
            "paused_for_quota", status="partial", pages=0,
            previous={"backfill_complete": True, "backfill_depth_reason": "reached_first_tweet"},
        )

        self.assertNotIn("backfill_complete", saved)
        self.assertNotIn("backfill_depth_reason", saved)

    def test_a_completed_walk_records_the_floor_it_was_judged_against(self):
        """Lowering the floor invalidates every reached_date_floor verdict;
        without this there is no way to find them again."""
        from datetime import datetime as _dt

        from fetcher.processing import TZ

        saved = self._record(
            "success_window_complete", cutoff=_dt(2024, 1, 1, tzinfo=TZ)
        )

        self.assertEqual(saved["backfill_floor_date"], "2024-01-01")

    def test_a_tick_that_fetched_nothing_for_any_other_reason_is_a_stall(self):
        saved = self._record(
            "failed_initial_auth", status="failed", pages=0, previous={"backfill_stalled_ticks": 2},
        )

        self.assertEqual(saved["backfill_stalled_ticks"], 3)

    def test_progress_clears_the_stall_counter(self):
        saved = self._record(
            "partial_safety_cap_reached", status="partial", pages=25,
            previous={"backfill_stalled_ticks": 4},
        )

        self.assertEqual(saved["backfill_stalled_ticks"], 0)


    def test_a_wall_probe_that_collected_tweets_reopens_the_walk(self):
        """The wall moved. Parking must not hide the newly served pages."""
        saved = self._record(
            "partial_safety_cap_reached", status="partial", pages=3,
            previous={
                "backfill_complete": True,
                "backfill_depth_reason": "provider_depth_limit",
                "backfill_cursor": "c9",
            },
        )

        self.assertIs(saved["backfill_complete"], False)
        self.assertIsNone(saved["backfill_depth_reason"])

    def test_a_wall_probe_that_never_talked_to_x_does_not_start_the_month_clock(self):
        """Quota pause or a failed first request is not a verdict."""
        saved = self._record(
            "paused_for_quota", status="partial", pages=0,
            previous={
                "backfill_complete": True,
                "backfill_depth_reason": "provider_depth_limit",
                "backfill_cursor": "c9",
                "backfill_completed_at": "old",
            },
        )

        self.assertNotIn("backfill_completed_at", saved)
        self.assertNotIn("backfill_complete", saved)


class ArchiveFloorTests(unittest.TestCase):
    """The date floor, exercised against a real tweet timestamp.

    Integration level on purpose, over the two units that have to agree: the
    floor is parsed in historical.py and compared in processing.py, and the
    only bug that matters here lives in the seam between them. Testing the
    parser alone is what let a naive datetime ship -- it looked correct in
    isolation and raised TypeError the moment it met a real tweet.
    """

    def _cutoff(self, value):
        import os
        from unittest.mock import patch as _patch

        from fetcher.historical import _archive_cutoff

        with _patch.dict(os.environ, {"TDF_ARCHIVE_EARLIEST_DATE": value}):
            return _archive_cutoff()

    def _coverage(self, cutoff, raw_timestamp):
        from fetcher.processing import RollingWindowEvaluator

        return RollingWindowEvaluator().evaluate_tweets_cutoff(
            [{"raw_timestamp": raw_timestamp}], cutoff=cutoff
        )

    def test_the_floor_can_actually_be_compared_to_a_tweet(self):
        """Regression: a naive floor raised inside the per-page coverage check,
        killing the walk before any cursor was stored -- so every tick restarted
        from page 1 and re-spent the shared request budget."""
        coverage = self._coverage(self._cutoff("2024-01-01"), "Wed Oct 10 20:19:24 +0000 2018")

        self.assertTrue(coverage.complete)
        self.assertEqual(coverage.reason, "cutoff_crossed")

    def test_a_tweet_newer_than_the_floor_does_not_end_the_walk(self):
        coverage = self._coverage(self._cutoff("2024-01-01"), "Wed Oct 10 20:19:24 +0000 2025")

        self.assertFalse(coverage.complete)
        self.assertEqual(coverage.reason, "cutoff_not_crossed")

    def test_a_malformed_floor_falls_back_to_the_default_not_to_no_floor(self):
        """No floor is the one outcome the setting exists to prevent."""
        from fetcher.historical import DEFAULT_ARCHIVE_EARLIEST_DATE

        cutoff = self._cutoff("01-01-2024")

        self.assertIsNotNone(cutoff)
        self.assertEqual(cutoff.strftime("%Y-%m-%d"), DEFAULT_ARCHIVE_EARLIEST_DATE)
        self.assertIsNotNone(cutoff.tzinfo)

    def test_an_unset_floor_means_no_floor(self):
        self.assertIsNone(self._cutoff(""))
