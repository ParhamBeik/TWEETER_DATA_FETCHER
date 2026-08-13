"""Policy tests for proven endpoint transport choices (2026-08-10)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tweeter_data_fetcher.pipelines.search.service import SearchTimelineMonitor
from tweeter_data_fetcher.x_api.timeline import FetcherEngine, _response_latency_ms


class ResponseLatencyTests(unittest.TestCase):
    def test_transport_latency_excludes_rate_limit_wait(self):
        response = SimpleNamespace(elapsed=SimpleNamespace(total_seconds=lambda: 1.25))

        self.assertEqual(_response_latency_ms(response, request_started=0), 1250)


class RepliesTransportPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        config_path = Path(self.temp_dir) / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "api_config": {
                        "user_tweets_query_id": "ut",
                        "user_tweets_and_replies_query_id": "ur",
                        "search_timeline_query_id": "st",
                        "user_by_screen_name_query_id": "ub",
                        "pagination_safety_cap_pages": 8,
                    },
                    "anti_bot_simulation": {
                        "delays_seconds": {
                            "replies_chunk_pages": 2,
                            "replies_retry_min": 0.01,
                            "replies_retry_max": 0.02,
                            "between_pages_replies_min": 0.01,
                            "between_pages_replies_max": 0.02,
                            "between_accounts_replies_min": 0.01,
                            "between_accounts_replies_max": 0.02,
                        },
                        "error_retry_policy": {
                            "client_error_attempts": 2,
                            "client_error_min_seconds": 0.01,
                            "client_error_max_seconds": 0.02,
                            "server_error_attempts": 1,
                            "request_error_attempts": 1,
                        },
                    },
                    "cookies": {"auth_token": "x", "ct0": "y"},
                    "default_timeout_seconds": 5,
                }
            )
        )
        self.engine = FetcherEngine(config_path=str(config_path), subsystem="test")
        self.engine.storage_manager = MagicMock()
        self.engine.storage_manager.create_raw_batch_dir.return_value = Path(self.temp_dir) / "batch"
        (Path(self.temp_dir) / "batch").mkdir(parents=True, exist_ok=True)
        self.engine.logger = MagicMock()
        self.engine.recorder = MagicMock()
        self.engine.api_manager.get_query_id = MagicMock(return_value="ur")
        self.engine.api_manager.retry_policy = MagicMock(
            return_value={
                "client_error_attempts": 2,
                "client_error_min_seconds": 0.01,
                "client_error_max_seconds": 0.02,
                "server_error_attempts": 1,
                "request_error_attempts": 1,
            }
        )
        self.engine.api_manager.jitter_sleep = MagicMock(return_value=0.01)
        self.engine.api_manager.reset_transport_session = MagicMock()
        self.engine.api_manager.rate_limits = {}
        self.engine.api_manager.simulation_config = {
            "delays_seconds": {
                "replies_chunk_pages": 2,
                "replies_retry_min": 0.01,
                "replies_retry_max": 0.02,
            }
        }

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_replies_initial_density_404_skips_browser(self):
        empty_404 = SimpleNamespace(status_code=404, text="", headers={}, request=SimpleNamespace(headers={}))
        self.engine.api_manager.perform_get = MagicMock(return_value=empty_404)
        self.engine.bootstrap_browser_context = MagicMock()

        result = self.engine._fetch_endpoint_result(
            account="chigrl",
            user_id="20253024",
            endpoint="UserTweetsAndReplies",
            max_pages=4,
            window_days=None,
            force_refetch=True,
        )

        self.assertEqual(result["outcome"], "failed_replies_density_404")
        self.assertEqual(result["transport"], "http")
        self.assertEqual(result["pages_fetched"], 0)
        self.engine.bootstrap_browser_context.assert_not_called()

    def test_expired_auth_stops_without_browser_or_retry(self):
        unauthorized = SimpleNamespace(
            status_code=401,
            text="Unauthorized",
            headers={},
            request=SimpleNamespace(headers={}),
        )
        self.engine.api_manager.perform_get = MagicMock(return_value=unauthorized)
        self.engine.bootstrap_browser_context = MagicMock()

        result = self.engine._fetch_endpoint_result(
            account="chigrl",
            user_id="20253024",
            endpoint="UserTweets",
            max_pages=4,
            window_days=None,
            force_refetch=True,
        )

        self.assertEqual(result["outcome"], "auth_required")
        self.assertEqual(self.engine.api_manager.perform_get.call_count, 1)
        self.engine.bootstrap_browser_context.assert_not_called()


class SearchCursorGateTests(unittest.TestCase):
    def test_mid_pagination_404_returns_cursor_gate_without_retry_sleep(self):
        monitor = SearchTimelineMonitor.__new__(SearchTimelineMonitor)
        monitor.api_manager = MagicMock()
        monitor.api_manager.retry_policy.return_value = {
            "client_error_attempts": 5,
            "client_error_min_seconds": 10,
            "client_error_max_seconds": 20,
            "server_error_attempts": 3,
            "request_error_attempts": 3,
            "rate_limit_safety_buffer_seconds": 1,
        }
        monitor.api_manager.jitter_sleep = MagicMock()
        monitor.api_manager.perform_get = MagicMock(
            return_value=SimpleNamespace(status_code=404, text="", headers={})
        )
        monitor.fetcher = MagicMock()
        monitor.fetcher.max_cursor_error_retries = 3
        monitor.fetcher.bootstrap_browser_context = MagicMock()
        monitor._build_frozen_headers = MagicMock(return_value={})
        monitor._compact_json = lambda x: "{}"
        monitor.config = {}

        with patch("tweeter_data_fetcher.pipelines.search.service.time.sleep") as sleep:
            out = monitor._request_page(
                "https://x.com/i/api/graphql/st/SearchTimeline",
                {"rawQuery": "q"},
                "{}",
                {},
                "cursor-from-p1",
                5,
                has_pages=True,
                search_url="https://x.com/search?q=q&f=live",
                browser_fallback_pages=2,
            )

        self.assertTrue(out.get("_cursor_gate"))
        self.assertEqual(out.get("_status"), 404)
        monitor.api_manager.jitter_sleep.assert_not_called()
        sleep.assert_not_called()
        monitor.fetcher.bootstrap_browser_context.assert_not_called()


if __name__ == "__main__":
    unittest.main()
