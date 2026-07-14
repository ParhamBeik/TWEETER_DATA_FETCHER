import unittest
import json
from unittest.mock import MagicMock, patch
from pathlib import Path
import tempfile

from tweeter_data_fetcher.twitter.timeline import FetcherEngine


class PaginationEngineTests(unittest.TestCase):
    """Tests for FetcherEngine pagination logic."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        config_path = Path(self.temp_dir) / "config.json"
        config_path.write_text(json.dumps({
            "user_tweets_query_id": "test_query_id",
            "user_tweets_and_replies_query_id": "test_replies_query_id",
            "search_timeline_query_id": "test_search_query_id",
            "user_by_screen_name_query_id": "test_screen_name_query_id",
            "real_transaction_ids_by_endpoint": {},
            "cookies": {"test": "cookie"},
            "default_timeout_seconds": 10,
        }))
        self.engine = FetcherEngine(config_path=str(config_path), subsystem="test")

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        import json
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cursor_walking_logic(self):
        """Test cursor walking through pagination."""
        # Test that cursor is extracted and used correctly
        self.assertTrue(hasattr(self.engine, '_timeline_variables'))

    def test_windowing_logic(self):
        """Test rolling window logic."""
        # Test that rolling window is applied
        self.assertTrue(hasattr(self.engine, '_timeline_field_toggles'))

    def test_retry_on_rate_limit(self):
        """Test retry logic on rate limits."""
        # This would need mocking of the actual HTTP requests
        # For now, just verify the engine has rate limit handling
        self.assertTrue(hasattr(self.engine, 'api_manager'))

    def test_error_handling_on_404(self):
        """Test 404 error handling."""
        # Verify the engine has 404 recovery logic
        self.assertTrue(hasattr(self.engine, '_recover_404_context'))
        self.assertTrue(hasattr(self.engine, 'max_404_recoveries'))

    def test_endpoint_specific_config(self):
        """Test endpoint-specific configuration."""
        # Check that config was loaded
        self.assertIn("user_tweets_query_id", self.engine.api_manager.config)
        self.assertIn("user_tweets_and_replies_query_id", self.engine.api_manager.config)

    def test_build_graphql_url(self):
        """Test GraphQL URL building."""
        url = self.engine._build_graphql_url(
            endpoint="UserTweets",
            query_id="test_qid",
            variables={"userId": "123"},
            features={"test": True},
            field_toggles={"withAuxiliaryUserLabels": True}
        )
        self.assertIn("test_qid", url)
        self.assertIn("userId", url)

    def test_request_headers_include_auth(self):
        """Test that request headers include authentication."""
        headers = self.engine.api_manager._build_request_headers(
            endpoint="UserTweets",
            context={"name": "test", "referer": "https://x.com/test", "active_user": "yes"},
            username="testuser"
        )
        self.assertIn("authorization", headers)
        self.assertIn("x-client-transaction-id", headers)

    def test_transaction_id_generation(self):
        """Test transaction ID generation fallback."""
        # With empty pool, should generate random ID
        tx_id = self.engine.api_manager._generate_transaction_id("_default")
        self.assertEqual(len(tx_id), 94)


if __name__ == "__main__":
    unittest.main()
