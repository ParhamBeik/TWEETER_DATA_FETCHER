import json
import sys
import types
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

# The contract builder itself does not use timezone behavior. Keep this unit
# test runnable in minimal environments where the optional runtime dependency
# is absent, without weakening production imports.
try:
    import pytz  # noqa: F401
except ImportError:
    pytz_stub = types.ModuleType("pytz")
    pytz_stub.timezone = lambda _name: None
    pytz_stub.UTC = None
    sys.modules["pytz"] = pytz_stub

from tests.diagnostics import traffic_sniffer as sniff_graphql
from src.shared.core.pagination_engine import FetcherEngine


class UserByScreenNameContractTests(unittest.TestCase):
    def test_builder_matches_captured_contract(self):
        engine = FetcherEngine.__new__(FetcherEngine)
        url, contract = engine.build_user_by_screen_name_url("example", "query-id")
        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        self.assertEqual(parsed.path, "/i/api/graphql/query-id/UserByScreenName")
        self.assertEqual(
            json.loads(params["variables"][0]),
            {"screen_name": "example", "withGrokTranslatedBio": True},
        )
        self.assertEqual(
            json.loads(params["features"][0]),
            {
                "hidden_profile_subscriptions_enabled": True,
                "profile_label_improvements_pcf_label_in_post_enabled": True,
                "responsive_web_profile_redirect_enabled": False,
                "rweb_tipjar_consumption_enabled": False,
                "verified_phone_label_enabled": False,
                "subscriptions_verification_info_is_identity_verified_enabled": True,
                "subscriptions_verification_info_verified_since_enabled": True,
                "highlights_tweets_tab_ui_enabled": True,
                "responsive_web_twitter_article_notes_tab_enabled": True,
                "subscriptions_feature_can_gift_premium": True,
                "creator_subscriptions_tweet_preview_api_enabled": True,
                "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
                "responsive_web_graphql_timeline_navigation_enabled": True,
            },
        )
        self.assertEqual(
            json.loads(params["fieldToggles"][0]),
            {"withPayments": False, "withAuxiliaryUserLabels": True},
        )
        self.assertEqual(contract["variables"], json.loads(params["variables"][0]))


class SnifferDefaultTests(unittest.TestCase):
    def test_no_argument_run_uses_configured_home_and_cookies(self):
        with patch.object(sniff_graphql, "observe") as observe:
            self.assertEqual(sniff_graphql.main([]), 0)

        self.assertEqual(observe.call_args.args, (sniff_graphql.START_URL,))
        self.assertTrue(observe.call_args.kwargs["load_config_cookies"])
        self.assertEqual(
            observe.call_args.kwargs["endpoint_allowlist"],
            sniff_graphql.CAPTURE_ENDPOINTS,
        )


if __name__ == "__main__":
    unittest.main()
