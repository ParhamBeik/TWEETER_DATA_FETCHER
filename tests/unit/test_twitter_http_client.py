import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tweeter_data_fetcher.twitter.client import APIManager


class TransactionIdTests(unittest.TestCase):
    def test_generated_transaction_id_matches_browser_length(self):
        manager = APIManager.__new__(APIManager)
        # Initialize required attributes for _generate_transaction_id
        manager.real_tx_ids = {}
        manager.tx_id_state = {}
        manager.tx_id_indices = {}

        self.assertEqual(len(manager._generate_transaction_id()), 94)

    def test_request_headers_refresh_transaction_id_per_request(self):
        manager = APIManager.__new__(APIManager)
        manager.session = SimpleNamespace(headers={"x-client-transaction-id": "old"})
        # Initialize required attributes for _generate_transaction_id
        manager.real_tx_ids = {}
        manager.tx_id_state = {}
        manager.tx_id_indices = {}

        context = {"name": "test", "referer": "https://x.com/example", "active_user": "yes"}
        first = manager._build_request_headers("UserTweets", context=context, username="example")
        second = manager._build_request_headers("UserTweets", context=context, username="example")

        self.assertEqual(len(first["x-client-transaction-id"]), 94)
        self.assertEqual(len(second["x-client-transaction-id"]), 94)
        self.assertNotEqual(first["x-client-transaction-id"], "old")
        self.assertNotEqual(first["x-client-transaction-id"], second["x-client-transaction-id"])


class ConfigPathResolutionTests(unittest.TestCase):
    def test_resolves_repo_relative_config_path(self):
        expected = Path(__file__).resolve().parents[2] / "config" / "config.example.json"
        resolved = APIManager._resolve_config_path("config/config.example.json")
        self.assertEqual(resolved, expected)

    def test_resolves_default_canonical_config_path(self):
        expected = Path(__file__).resolve().parents[2] / "config" / "config.json"
        resolved = APIManager._resolve_config_path(None)
        self.assertEqual(resolved, expected)


class AutoRefreshTests(unittest.TestCase):
    def test_auto_refresh_only_runs_once_per_endpoint_account(self):
        manager = APIManager.__new__(APIManager)
        manager.config_path = Path("/tmp/config.json")
        manager.real_tx_ids = {"UserTweets": ["tx-1"]}
        manager.tx_id_state = {"UserTweets": {"tx-1": {"status": "healthy", "failures": 0}}}
        manager.query_id_pools = {"UserTweets": ["q-1"]}
        manager.query_id_state = {"UserTweets": {"q-1": {"status": "healthy", "failures": 0}}}
        manager.endpoint_health = {}
        manager.consecutive_404s = {}
        manager.auto_refresh_attempts = {}
        manager.recorder = MagicMock()
        manager.refresh_config_and_query_ids = lambda: None
        manager._save_tx_id_state = lambda: None
        manager._save_query_id_state = lambda: None
        manager._mark_tx_id = lambda *args, **kwargs: None
        manager._mark_query_id = lambda *args, **kwargs: None

        with patch("tweeter_data_fetcher.twitter.auth.auto_refresh_session", return_value=True) as refresh_mock:
            first = manager._auto_refresh_params("UserTweets", username="elonmusk")
            second = manager._auto_refresh_params("UserTweets", username="elonmusk")

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(refresh_mock.call_count, 1)
        manager.recorder.emit_auto_refresh_start.assert_called_once()
        manager.recorder.emit_auto_refresh_done.assert_called_once_with(
            endpoint="UserTweets",
            updated=["UserTweets"],
            success=True,
            username="elonmusk",
        )


class QueryIdSelectionTests(unittest.TestCase):
    def test_pinned_query_id_is_reused_until_ruled_out(self):
        manager = APIManager.__new__(APIManager)
        manager.query_ids = {"SearchTimeline": "fresh-id"}
        manager.query_id_pools = {"SearchTimeline": ["stale-id", "fresh-id"]}
        manager.query_id_state = {
            "SearchTimeline": {
                "fresh-id": {"status": "healthy", "failures": 0},
                "stale-id": {"status": "stale", "failures": 3},
            }
        }
        manager.query_id_indices = {"SearchTimeline": 0}

        self.assertEqual(manager._next_query_id("SearchTimeline"), "fresh-id")
        self.assertEqual(manager._next_query_id("SearchTimeline"), "fresh-id")


if __name__ == "__main__":
    unittest.main()
