import unittest
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from fetcher.live import LiveMonitor
from fetcher.search import SearchTimelineMonitor
from fetcher.config import DEFAULT_PRIORITY_POLICIES


class LiveStatusTests(unittest.TestCase):
    def test_live_account_fails_when_required_endpoint_fails(self):
        monitor = LiveMonitor.__new__(LiveMonitor)
        monitor.account_map = {}
        monitor.priority_policies = DEFAULT_PRIORITY_POLICIES
        monitor.console = MagicMock()
        monitor.live_storage = MagicMock()
        monitor.live_storage.update_account_state.return_value = None
        monitor.fetcher = MagicMock()
        monitor.fetcher.bootstrap_browser_context.return_value = MagicMock(
            ok=True,
            route="https://x.com/example",
            support_request_count=1,
            error=None,
        )
        monitor._get_live_user_id = MagicMock(return_value="1")
        monitor._fetch_live_endpoint = MagicMock(
            return_value={"endpoint": "UserTweets", "status": "failed", "pages": []}
        )
        monitor._process_sets = MagicMock(return_value={"4_union": []})
        monitor._handle_new_tweets = MagicMock(return_value={"new": 0})

        result = monitor.monitor_account("example")

        self.assertEqual(result["status"], "failed")


class SearchStatusTests(unittest.TestCase):
    def test_search_state_does_not_sleep_failed_initial_404(self):
        monitor = SearchTimelineMonitor.__new__(SearchTimelineMonitor)
        monitor.storage = MagicMock()
        monitor.storage._tehran_now.return_value = datetime.utcnow()
        monitor.storage._jalali_batch_name.return_value = "batch"
        monitor.config = {}
        monitor.console = MagicMock()
        monitor.api_manager = MagicMock()
        monitor.api_manager.get_query_id.return_value = "qid"
        monitor.api_manager.rate_limits = {}
        monitor.fetcher = MagicMock()
        monitor.fetcher.first_request_warmup_seconds = 0
        monitor.fetcher.bootstrap_browser_context.return_value = MagicMock(
            ok=True,
            route="https://x.com/search",
            support_request_count=1,
            error=None,
        )
        monitor.raw_root = Path("/tmp/nonexistent-search-raw")
        monitor.reports_root = Path("/tmp/nonexistent-search-reports")
        monitor.search_state = {"test::latest": {"last_checked_at": "old"}}
        monitor.state_file = Path("/tmp/nonexistent-search-state.json")
        monitor._policy_for_search = MagicMock(return_value={
            "rolling_hours": 24,
            "pagination_safety_cap_pages": 1,
            "max_retries": 1,
        })
        monitor._raw_batch_dir = MagicMock(return_value=Path("/tmp/nonexistent-search-batch"))
        monitor._build_frozen_headers = MagicMock(return_value={})
        monitor._save_exports = MagicMock(return_value={})
        monitor._save_json = MagicMock()
        monitor._request_page = MagicMock(return_value={
            "_failure": "failed_initial_404",
            "_status": 404,
            "_attempts": 1,
            "_error_samples": [],
        })

        report = monitor.monitor_search({"name": "test", "raw_query": "test", "product": "Latest"})

        self.assertEqual(report["status"], "failed")
        self.assertEqual(monitor.search_state["test::latest"]["last_checked_at"], "old")

    def test_search_uses_configured_features(self):
        monitor = SearchTimelineMonitor.__new__(SearchTimelineMonitor)
        monitor.storage = MagicMock()
        monitor.storage._tehran_now.return_value = datetime.utcnow()
        monitor.storage._jalali_batch_name.return_value = "batch"
        monitor.config = {
            "graphql_endpoint_payloads": {
                "SearchTimeline": {
                    "features": {"from_config": True},
                },
            },
            "api_config": {"search_warmup_seconds": 0},
        }
        monitor.console = MagicMock()
        monitor.api_manager = MagicMock()
        monitor.api_manager.get_query_id.return_value = "qid"
        monitor.api_manager.rate_limits = {}
        monitor.fetcher = MagicMock()
        monitor.fetcher.first_request_warmup_seconds = 0
        monitor.fetcher.bootstrap_browser_context.return_value = MagicMock(ok=True)
        monitor.raw_root = Path("/tmp/nonexistent-search-raw")
        monitor.reports_root = Path("/tmp/nonexistent-search-reports")
        monitor.search_state = {}
        monitor.state_file = Path("/tmp/nonexistent-search-state.json")
        monitor._policy_for_search = MagicMock(return_value={
            "rolling_hours": 24,
            "pagination_safety_cap_pages": 1,
            "max_retries": 1,
        })
        monitor._raw_batch_dir = MagicMock(return_value=Path("/tmp/nonexistent-search-batch"))
        monitor._build_frozen_headers = MagicMock(return_value={})
        monitor._save_exports = MagicMock(return_value={})
        monitor._save_json = MagicMock()
        monitor.api_manager.warmup_url = MagicMock()
        monitor._request_page = MagicMock(return_value={
            "_failure": "failed_initial_404",
            "_status": 404,
            "_attempts": 1,
            "_error_samples": [],
        })

        monitor.monitor_search({"name": "test", "raw_query": "test", "product": "Latest"})

        features_json = monitor._request_page.call_args.args[2]
        self.assertEqual(json.loads(features_json), {"from_config": True})


if __name__ == "__main__":
    unittest.main()
