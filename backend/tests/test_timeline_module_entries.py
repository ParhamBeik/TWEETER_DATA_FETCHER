"""Self-threads must survive extraction.

X serves a profile timeline as two entry shapes: a plain item holding one tweet,
and a `TimelineTimelineModule` -- a self-thread -- holding several under
`content.items[].item.itemContent`. Only the first was ever reachable: the item
lookup ran first and `continue`d the whole entry when it came back empty, which
is exactly what a module does, so every threaded post was dropped from every page
of every account.

Measured against production before the fix: eight pages of @geoconfirmed yielded
3 tweets and four "empty" pages; after it, the same eight pages yielded 140
tweets and no empty pages. The empty pages were the second casualty -- a page
holding only conversations extracted nothing, looked like the end of the
timeline, and parked the account as "X's serving depth" after two pages.
"""
from __future__ import annotations

import unittest

from engine.processing import TweetSetProcessor


def _legacy(tweet_id: str) -> dict:
    return {
        "rest_id": tweet_id,
        "core": {"user_results": {"result": {"rest_id": "42", "core": {"screen_name": "geoconfirmed"}}}},
        "legacy": {
            "full_text": f"tweet {tweet_id}",
            "created_at": "Sat Sep 05 20:22:35 +0000 2026",
        },
    }


def _item_entry(tweet_id: str) -> dict:
    return {
        "entryId": f"tweet-{tweet_id}",
        "content": {
            "entryType": "TimelineTimelineItem",
            "itemContent": {"itemType": "TimelineTweet", "tweet_results": {"result": _legacy(tweet_id)}},
        },
    }


def _module_entry(entry_id: str, tweet_ids: list[str]) -> dict:
    """The shape X actually returns for a self-thread (verified against live X)."""
    return {
        "entryId": f"profile-conversation-{entry_id}",
        "content": {
            "entryType": "TimelineTimelineModule",
            "items": [
                {
                    "entryId": f"conversation-{entry_id}-{tweet_id}",
                    "item": {
                        "itemContent": {
                            "itemType": "TimelineTweet",
                            "tweet_results": {"result": _legacy(tweet_id)},
                        }
                    },
                }
                for tweet_id in tweet_ids
            ],
        },
    }


def _page(entries: list[dict]) -> dict:
    return {
        "data": {"user": {"result": {"timeline": {"timeline": {"instructions": [
            {"type": "TimelineAddEntries", "entries": entries}
        ]}}}}}
    }


class ModuleEntryExtraction(unittest.TestCase):
    def test_conversation_modules_yield_every_tweet(self):
        page = _page([_module_entry("c1", ["1", "2", "3"])])

        extracted = TweetSetProcessor().extract_tweets_from_raw(
            [page], username="geoconfirmed", source_endpoint="UserTweets"
        )

        self.assertEqual({"42:1", "42:2", "42:3"}, set(extracted))

    def test_a_page_of_only_conversations_is_not_empty(self):
        """The precise condition that produced the false 'timeline exhausted'."""
        page = _page([_module_entry("c1", ["1", "2"]), _module_entry("c2", ["3"])])

        extracted = TweetSetProcessor().extract_tweets_from_raw(
            [page], username="geoconfirmed", source_endpoint="UserTweets"
        )

        self.assertEqual(3, len(extracted))

    def test_plain_items_still_extract(self):
        page = _page([_item_entry("1"), _item_entry("2")])

        extracted = TweetSetProcessor().extract_tweets_from_raw(
            [page], username="geoconfirmed", source_endpoint="UserTweets"
        )

        self.assertEqual({"42:1", "42:2"}, set(extracted))

    def test_mixed_page_extracts_both_shapes_and_dedupes(self):
        page = _page([
            _item_entry("1"),
            _module_entry("c1", ["2", "3"]),
            _item_entry("3"),  # same tweet as the module's last -- one row, not two
        ])

        extracted = TweetSetProcessor().extract_tweets_from_raw(
            [page], username="geoconfirmed", source_endpoint="UserTweets"
        )

        self.assertEqual({"42:1", "42:2", "42:3"}, set(extracted))

    def test_cursor_only_page_still_extracts_nothing(self):
        """The stop signal must keep working where it is genuinely correct."""
        page = _page([
            {"entryId": "cursor-bottom-1", "content": {
                "entryType": "TimelineTimelineCursor", "cursorType": "Bottom", "value": "abc"}},
        ])

        extracted = TweetSetProcessor().extract_tweets_from_raw(
            [page], username="geoconfirmed", source_endpoint="UserTweets"
        )

        self.assertEqual({}, extracted)

    def test_malformed_module_items_are_skipped_not_fatal(self):
        page = _page([{
            "entryId": "profile-conversation-broken",
            "content": {"entryType": "TimelineTimelineModule", "items": [
                "not-a-dict",
                {"item": None},
                {"item": {"itemContent": None}},
                {"item": {"itemContent": {"tweet_results": {"result": _legacy("9")}}}},
            ]},
        }])

        extracted = TweetSetProcessor().extract_tweets_from_raw(
            [page], username="geoconfirmed", source_endpoint="UserTweets"
        )

        self.assertEqual({"42:9"}, set(extracted))


if __name__ == "__main__":
    unittest.main()
