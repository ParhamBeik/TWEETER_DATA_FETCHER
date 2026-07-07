import unittest
from types import SimpleNamespace

from shared.core.twitter_http_client import APIManager


class TransactionIdTests(unittest.TestCase):
    def test_generated_transaction_id_matches_browser_length(self):
        manager = APIManager.__new__(APIManager)

        self.assertEqual(len(manager._generate_transaction_id()), 94)

    def test_request_headers_refresh_transaction_id_per_request(self):
        manager = APIManager.__new__(APIManager)
        manager.session = SimpleNamespace(headers={"x-client-transaction-id": "old"})

        context = {"name": "test", "referer": "https://x.com/example", "active_user": "yes"}
        first = manager._build_request_headers("UserTweets", context=context, username="example")
        second = manager._build_request_headers("UserTweets", context=context, username="example")

        self.assertEqual(len(first["x-client-transaction-id"]), 94)
        self.assertEqual(len(second["x-client-transaction-id"]), 94)
        self.assertNotEqual(first["x-client-transaction-id"], "old")
        self.assertNotEqual(first["x-client-transaction-id"], second["x-client-transaction-id"])


if __name__ == "__main__":
    unittest.main()
