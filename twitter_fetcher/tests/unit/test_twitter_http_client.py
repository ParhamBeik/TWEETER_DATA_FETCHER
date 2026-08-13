import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from tweeter_data_fetcher.x_api.client import APIManager, EndpointHealth


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

    def test_browser_transaction_id_is_pinned_for_endpoint_run(self):
        manager = APIManager.__new__(APIManager)
        manager.session = SimpleNamespace(
            headers={"x-client-transaction-id": "configured-id"},
            cookies=MagicMock(),
        )
        manager.config = {"api_config": {}}
        manager.real_tx_ids = {"UserTweetsAndReplies": ["configured-id"]}
        manager.browser_tx_ids = {}
        manager.tx_id_state = {}
        manager.tx_id_indices = {}
        manager.query_ids = {}
        manager.query_id_pools = {}

        manager.apply_browser_context(
            request_headers={
                "UserTweetsAndReplies": {
                    "x-client-transaction-id": "browser-confirmed-id",
                }
            }
        )

        first = manager._build_request_headers("UserTweetsAndReplies", username="example")
        second = manager._build_request_headers("UserTweetsAndReplies", username="example")
        self.assertEqual(first["x-client-transaction-id"], "browser-confirmed-id")
        self.assertEqual(second["x-client-transaction-id"], "browser-confirmed-id")


class ConfigPathResolutionTests(unittest.TestCase):
    def test_resolves_repo_relative_config_path(self):
        expected = Path(__file__).resolve().parents[2] / "config" / "config.example.json"
        resolved = APIManager._resolve_config_path("config/config.example.json")
        self.assertEqual(resolved, expected)

    def test_resolves_default_canonical_config_path(self):
        expected = Path(__file__).resolve().parents[2] / "config" / "config.json"
        resolved = APIManager._resolve_config_path(None)
        self.assertEqual(resolved, expected)


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

    def test_get_query_id_does_not_discard_browser_capture(self):
        manager = APIManager.__new__(APIManager)
        manager.query_ids = {"SearchTimeline": "browser-id"}
        manager.query_id_pools = {"SearchTimeline": ["browser-id", "disk-id"]}
        manager.query_id_state = {"SearchTimeline": {}}
        manager.query_id_indices = {}
        manager.refresh_config_and_query_ids = MagicMock()

        self.assertEqual(manager.get_query_id("SearchTimeline"), "browser-id")
        manager.refresh_config_and_query_ids.assert_not_called()


class ResponseClassificationTests(unittest.TestCase):
    def setUp(self):
        self.manager = APIManager.__new__(APIManager)
        self.manager.last_status_by_endpoint = {}
        self.manager.endpoint_health = {}
        self.manager.consecutive_404s = {}
        self.manager.update_rate_limit = MagicMock()
        self.manager._save_endpoint_health = MagicMock()
        self.manager._mark_tx_id = MagicMock()
        self.manager._mark_query_id = MagicMock()

    def classify(self, status):
        response = SimpleNamespace(status_code=status, headers={})
        self.manager._process_response_status(
            "SearchTimeline",
            response,
            {"x-client-transaction-id": "tx-id"},
            "https://x.com/i/api/graphql/query-id/SearchTimeline",
        )

    def test_404_is_context_rejection_not_parameter_staleness(self):
        self.classify(404)

        self.assertEqual(self.manager.endpoint_health["SearchTimeline"], EndpointHealth.CONTEXT_REJECTED)
        self.manager._mark_tx_id.assert_not_called()
        self.manager._mark_query_id.assert_not_called()

    def test_contract_and_auth_failures_are_distinct(self):
        self.classify(400)
        self.assertEqual(self.manager.endpoint_health["SearchTimeline"], EndpointHealth.CONTRACT_REJECTED)
        self.classify(401)
        self.assertEqual(self.manager.endpoint_health["SearchTimeline"], EndpointHealth.AUTH_REQUIRED)


if __name__ == "__main__":
    unittest.main()
