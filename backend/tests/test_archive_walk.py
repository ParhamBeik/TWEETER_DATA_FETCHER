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


if __name__ == "__main__":
    unittest.main()


class ArchiveCompletionTests(unittest.TestCase):
    """Which outcomes may end an account's archive walk for good.

    Unit level on the bookkeeping function, because the distinction being
    guarded is a pure mapping from an outcome to a decision -- and getting it
    wrong is silent: the account simply never gets collected again.
    """

    def _record(self, outcome, *, status="completed", pages=5, previous=None):
        from fetcher.historical import _record_backfill_progress

        saved = {}
        storage = MagicMock()
        storage.update_endpoint_state = lambda account, endpoint, meta=None, **_: saved.update(meta or {})
        _record_backfill_progress(
            storage, "business", "UserTweets", previous or {},
            {"status": status, "outcome": outcome, "pages_fetched": pages, "last_cursor": "c9"},
        )
        return saved

    def test_covering_the_rolling_window_does_not_end_the_archive_walk(self):
        """The regression: this reports status=completed but a cursor remains.

        Treating it as the end marked active accounts permanently archived after
        a few pages, and left the resumable cursor unused.
        """
        saved = self._record("success_window_complete")

        self.assertNotIn("backfill_complete", saved)
        self.assertEqual(saved["backfill_cursor"], "c9")

    def test_running_out_of_tweets_ends_the_walk(self):
        saved = self._record("success_timeline_exhausted")

        self.assertIs(saved["backfill_complete"], True)
        self.assertIsNone(saved["backfill_cursor"])

    def test_reaching_the_true_end_of_pagination_ends_the_walk(self):
        saved = self._record("success_true_end")

        self.assertIs(saved["backfill_complete"], True)

    def test_pausing_for_quota_is_not_a_stall(self):
        """It is the design working. Counting it demotes healthy accounts that
        happen to sit late in a chunk that ran the shared bucket down."""
        saved = self._record(
            "paused_for_quota", status="partial", pages=0, previous={"backfill_stalled_ticks": 2},
        )

        self.assertEqual(saved["backfill_stalled_ticks"], 2)
        self.assertNotIn("backfill_complete", saved)

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
