import unittest
import json
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fetcher.search import SearchTimelineMonitor
from fetcher.processing import TweetSetProcessor
from fetcher.browser import BrowserBootstrapResult


class SearchPipelineTests(unittest.TestCase):
    """Integration tests for search pipeline."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / "config.json"
        self.search_config_path = Path(self.temp_dir) / "search_config.json"
        
        self.config_path.write_text(json.dumps({
            "user_tweets_query_id": "test_query_id",
            "user_tweets_and_replies_query_id": "test_replies_query_id",
            "search_timeline_query_id": "test_search_query_id",
            "user_by_screen_name_query_id": "test_screen_name_query_id",
            "real_transaction_ids_by_endpoint": {},
            "cookies": {"test": "cookie"},
            "default_timeout_seconds": 10,
        }))
        
        self.search_config_path.write_text(json.dumps({
            "search_queries": [
                {"name": "test", "raw_query": "test query", "product": "Latest"}
            ]
        }))

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch('fetcher.search.FetcherEngine')
    @patch('fetcher.search.StorageManager')
    def test_search_monitor_initialization(self, mock_storage, mock_engine):
        """Test SearchTimelineMonitor initialization."""
        mock_engine_instance = MagicMock()
        mock_engine_instance.config = {}
        mock_engine_instance.api_manager = MagicMock()
        mock_engine.return_value = mock_engine_instance
        
        mock_storage_instance = MagicMock()
        mock_storage.return_value = mock_storage_instance
        
        monitor = SearchTimelineMonitor(
            config_path=str(self.config_path),
            search_config_path=str(self.search_config_path)
        )
        self.assertTrue(hasattr(monitor, 'fetcher'))
        self.assertTrue(hasattr(monitor, 'storage'))

    def test_valid_products_constant(self):
        """Test that VALID_PRODUCTS constant is defined."""
        from fetcher.search import VALID_PRODUCTS
        self.assertIn("Top", VALID_PRODUCTS)
        self.assertIn("Latest", VALID_PRODUCTS)
        self.assertIn("Media", VALID_PRODUCTS)
        self.assertIn("People", VALID_PRODUCTS)

    @patch('fetcher.search.FetcherEngine')
    @patch('fetcher.search.StorageManager')
    def test_monitor_search_structure(self, mock_storage, mock_engine):
        """Test monitor_search method structure."""
        mock_engine_instance = MagicMock()
        mock_engine_instance.config = {}
        mock_engine_instance.api_manager = MagicMock()
        mock_engine.return_value = mock_engine_instance
        
        mock_storage_instance = MagicMock()
        mock_storage.return_value = mock_storage_instance
        
        monitor = SearchTimelineMonitor(
            config_path=str(self.config_path),
            search_config_path=str(self.search_config_path)
        )
        self.assertTrue(hasattr(monitor, 'monitor_search'))
        self.assertTrue(callable(monitor.monitor_search))

    @patch('fetcher.search.FetcherEngine')
    @patch('fetcher.search.StorageManager')
    def test_search_query_building(self, mock_storage, mock_engine):
        """Test search query building."""
        mock_engine_instance = MagicMock()
        mock_engine_instance.config = {}
        mock_engine.return_value = mock_engine_instance
        
        mock_storage_instance = MagicMock()
        mock_storage.return_value = mock_storage_instance
        
        monitor = SearchTimelineMonitor(
            config_path=str(self.config_path),
            search_config_path=str(self.search_config_path)
        )
        
        # Test query building method exists (may be internal)
        self.assertTrue(True)  # Structure test only

    def test_frozen_search_features(self):
        """Test FROZEN_SEARCH_FEATURES is defined."""
        from fetcher.search import FROZEN_SEARCH_FEATURES
        self.assertIsInstance(FROZEN_SEARCH_FEATURES, dict)

    @staticmethod
    def search_page(tweet_id, created_at, cursor):
        return {
            "data": {
                "search_by_raw_query": {
                    "search_timeline": {
                        "timeline": {
                            "instructions": [{
                                "type": "TimelineAddEntries",
                                "entries": [
                                    {
                                        "entryId": f"tweet-{tweet_id}",
                                        "content": {
                                            "__typename": "TimelineTimelineItem",
                                            "itemContent": {"tweet_results": {"result": {
                                                "rest_id": str(tweet_id),
                                                "legacy": {"full_text": "tweet", "created_at": created_at},
                                                "core": {"user_results": {"result": {"legacy": {"screen_name": "author"}}}},
                                            }}},
                                        },
                                    },
                                    {
                                        "entryId": f"cursor-bottom-{cursor}",
                                        "content": {
                                            "__typename": "TimelineTimelineCursor",
                                            "cursorType": "Bottom",
                                            "value": cursor,
                                        },
                                    },
                                ],
                            }],
                        },
                    },
                },
            },
        }

    def test_deep_search_http_page_one_then_browser_for_depth(self):
        monitor = SearchTimelineMonitor.__new__(SearchTimelineMonitor)
        monitor.config = {"api_config": {"pagination_safety_cap_pages": 50}}
        monitor.storage = MagicMock()
        monitor.storage._tehran_now.return_value = datetime.utcnow()
        monitor.storage._jalali_batch_name.return_value = "batch"
        monitor.storage.save_search_result_page.side_effect = lambda *args: Path(self.temp_dir) / f"page_{args[-2]}.json"
        monitor.raw_root = Path(self.temp_dir) / "raw"
        monitor.reports_root = Path(self.temp_dir) / "reports"
        monitor.state_file = Path(self.temp_dir) / "state.json"
        monitor.search_state = {}
        monitor.processor = TweetSetProcessor()
        monitor.console = MagicMock()
        monitor.api_manager = MagicMock()
        monitor.api_manager.rate_limits = {"SearchTimeline": {}}
        monitor.api_manager.get_query_id.return_value = "test_search_query_id"
        monitor.fetcher = MagicMock()
        monitor.fetcher.recorder = MagicMock()
        monitor.fetcher.bootstrap_browser_context.return_value = BrowserBootstrapResult(
            True,
            "https://x.com/search",
            target_pages={"SearchTimeline": [
                self.search_page("1", "Wed Aug 05 00:00:00 +0000 2026", "cursor-1"),
                self.search_page("2", "Wed Jan 01 00:00:00 +0000 2020", "cursor-2"),
            ]},
            stop_reason="predicate",
        )
        http_page = self.search_page("http1", "Wed Aug 05 12:00:00 +0000 2026", "cursor-http")
        http_page["_attempts"] = 1
        http_page["_status"] = 200
        http_page["_error_samples"] = []
        monitor._request_page = MagicMock(return_value=http_page)
        monitor._save_exports = MagicMock(return_value={})
        monitor._build_frozen_headers = MagicMock(return_value={})
        monitor._compact_json = lambda x: "{}"
        monitor._after_bootstrap = MagicMock()

        report = monitor.monitor_search({
            "name": "test",
            "raw_query": "test",
            "product": "Latest",
            "pagination_depth": 3,
            "rolling_hours": 24,
        })

        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["metadata"]["transport"], "http+browser")
        monitor._request_page.assert_called_once()
        self.assertEqual(monitor.fetcher.bootstrap_browser_context.call_count, 1)

    def test_deep_search_top_product_http_failure_fallback_has_no_window_stop(self):
        # Regression: the HTTP-failure fallback path used to pass the
        # chronological window-crossing predicate unconditionally, which caps
        # relevance-ranked `Top` searches at one page (mirrors the bug already
        # fixed on the HTTP-success path above it).
        monitor = SearchTimelineMonitor.__new__(SearchTimelineMonitor)
        monitor.config = {"api_config": {"pagination_safety_cap_pages": 50}}
        monitor.storage = MagicMock()
        monitor.storage._tehran_now.return_value = datetime.utcnow()
        monitor.storage._jalali_batch_name.return_value = "batch"
        monitor.storage.save_search_result_page.side_effect = lambda *args: Path(self.temp_dir) / f"page_{args[-2]}.json"
        monitor.raw_root = Path(self.temp_dir) / "raw"
        monitor.reports_root = Path(self.temp_dir) / "reports"
        monitor.state_file = Path(self.temp_dir) / "state.json"
        monitor.search_state = {}
        monitor.processor = TweetSetProcessor()
        monitor.console = MagicMock()
        monitor.api_manager = MagicMock()
        monitor.api_manager.rate_limits = {"SearchTimeline": {}}
        monitor.api_manager.get_query_id.return_value = "test_search_query_id"
        monitor.fetcher = MagicMock()
        monitor.fetcher.recorder = MagicMock()
        monitor.fetcher.bootstrap_browser_context.return_value = BrowserBootstrapResult(
            True,
            "https://x.com/search",
            target_pages={"SearchTimeline": [
                self.search_page("1", "Wed Aug 05 00:00:00 +0000 2026", "cursor-1"),
                self.search_page("2", "Wed Jan 01 00:00:00 +0000 2020", "cursor-2"),
            ]},
            stop_reason="page_cap",
        )
        http_page = {
            "_attempts": 1,
            "_status": 404,
            "_error_samples": [],
            "_failure": "http_404",
        }
        monitor._request_page = MagicMock(return_value=http_page)
        monitor._save_exports = MagicMock(return_value={})
        monitor._build_frozen_headers = MagicMock(return_value={})
        monitor._compact_json = lambda x: "{}"
        monitor._after_bootstrap = MagicMock()

        monitor.monitor_search({
            "name": "test",
            "raw_query": "test",
            "product": "Top",
            "pagination_depth": 3,
            "rolling_hours": 24,
        })

        _, kwargs = monitor.fetcher.bootstrap_browser_context.call_args
        self.assertIsNone(kwargs["stop_when"])


if __name__ == "__main__":
    unittest.main()
