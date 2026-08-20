import unittest

from fetcher.processing import (
    extract_bottom_cursor,
    search_timeline_variables,
    timeline_variables,
    validate_graphql_payload,
)


def user_timeline_payload(entries=None, errors=None):
    payload = {
        "data": {
            "user": {
                "result": {
                    "timeline": {
                        "timeline": {
                            "instructions": [
                                {
                                    "type": "TimelineAddEntries",
                                    "entries": entries or [],
                                }
                            ]
                        }
                    }
                }
            }
        }
    }
    if errors is not None:
        payload["errors"] = errors
    return payload


class GraphQLContractTests(unittest.TestCase):
    def test_rejects_graphql_errors_even_with_http_200_shape(self):
        validation = validate_graphql_payload(
            "UserTweets",
            user_timeline_payload(errors=[{"message": "boom"}]),
        )

        self.assertFalse(validation.ok)
        self.assertEqual(validation.reason, "graphql_errors")

    def test_rejects_missing_expected_data_path(self):
        validation = validate_graphql_payload("SearchTimeline", {"data": {"user": {}}})

        self.assertFalse(validation.ok)
        self.assertEqual(validation.reason, "expected_data_missing")

    def test_extracts_bottom_cursor_from_nested_entry(self):
        payload = user_timeline_payload(
            entries=[
                {
                    "entryId": "cursor-bottom-1",
                    "content": {"cursorType": "Bottom", "value": "CURSOR"},
                }
            ]
        )

        self.assertEqual(extract_bottom_cursor(payload), "CURSOR")

    def test_timeline_variables_are_endpoint_isolated(self):
        tweets = timeline_variables("UserTweets", "123", "abc")
        replies = timeline_variables("UserTweetsAndReplies", "123", "abc")

        self.assertIn("withQuickPromoteEligibilityTweetFields", tweets)
        self.assertNotIn("withCommunity", tweets)
        self.assertIn("withCommunity", replies)
        self.assertEqual(replies["cursor"], "abc")

    def test_search_variables_omit_cursor_until_pagination(self):
        initial = search_timeline_variables(raw_query="hello", product="Latest")
        cursor = search_timeline_variables(raw_query="hello", product="Latest", cursor="NEXT")

        self.assertNotIn("cursor", initial)
        self.assertEqual(cursor["cursor"], "NEXT")


if __name__ == "__main__":
    unittest.main()
