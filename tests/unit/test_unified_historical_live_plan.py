import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import pytz

from tweeter_data_fetcher.tweets._legacy import RollingWindowEvaluator, TweetSetProcessor, window_cutoff
from tweeter_data_fetcher.twitter.client import APIManager


def _tweet_at(dt: datetime) -> dict:
    return {"id": str(int(dt.timestamp())), "created_at": dt.strftime("%a %b %d %H:%M:%S %z %Y")}


class UnifiedHistoricalLivePlanTests(unittest.TestCase):
    def test_rolling_window_cutoff_is_timestamp_granular(self):
        tz = pytz.timezone("Asia/Tehran")
        cutoff = tz.localize(datetime(2026, 7, 7, 12, 0, 0))
        evaluator = RollingWindowEvaluator()

        self.assertTrue(evaluator.evaluate_tweets_cutoff([_tweet_at(cutoff - timedelta(seconds=1))], cutoff).complete)
        self.assertFalse(evaluator.evaluate_tweets_cutoff([_tweet_at(cutoff + timedelta(seconds=1))], cutoff).complete)

    def test_watermark_floors_before_configured_window(self):
        tz = pytz.timezone("Asia/Tehran")
        now = tz.localize(datetime(2026, 7, 7, 12, 0, 0))
        cutoff = window_cutoff(
            window_days=1,
            watermark="2026-07-05T14:00:00+03:30",
            floor="day",
            now_dt=now,
        )

        self.assertEqual(cutoff, tz.localize(datetime(2026, 7, 5, 0, 0, 0)))

    def test_seven_set_operations(self):
        processor = TweetSetProcessor()
        set_a = {"author:1": {"id": "1"}, "author:2": {"id": "2"}}
        set_b = {"author:2": {"id": "2"}, "author:3": {"id": "3"}}

        a_minus_b = processor.get_difference_a_minus_b(set_a, set_b)
        b_minus_a = processor.get_difference_b_minus_a(set_a, set_b)
        symmetric = processor.get_symmetric_difference(set_a, set_b)
        union = processor.get_union(set_a, set_b)

        self.assertEqual({tweet["id"] for tweet in a_minus_b}, {"1"})
        self.assertEqual({tweet["id"] for tweet in b_minus_a}, {"3"})
        self.assertEqual({tweet["id"] for tweet in symmetric}, {"1", "3"})
        self.assertEqual({tweet["id"] for tweet in union}, {"1", "2", "3"})

    def test_param_rule_out_after_three_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = APIManager.__new__(APIManager)
            manager.state_dir = Path(tmp)
            manager.tx_id_state = {}
            manager.query_id_state = {}

            manager._mark_tx_id("UserTweets", "tx", "stale")
            manager._mark_query_id("UserTweets", "qid", "stale")
            self.assertFalse(manager._param_ruled_out(manager.tx_id_state["UserTweets"]["tx"]))
            self.assertFalse(manager._param_ruled_out(manager.query_id_state["UserTweets"]["qid"]))

            manager._mark_tx_id("UserTweets", "tx", "stale")
            manager._mark_query_id("UserTweets", "qid", "stale")
            self.assertFalse(manager._param_ruled_out(manager.tx_id_state["UserTweets"]["tx"]))
            self.assertFalse(manager._param_ruled_out(manager.query_id_state["UserTweets"]["qid"]))

            manager._mark_tx_id("UserTweets", "tx", "stale")
            manager._mark_query_id("UserTweets", "qid", "stale")
            self.assertTrue(manager._param_ruled_out(manager.tx_id_state["UserTweets"]["tx"]))
            self.assertTrue(manager._param_ruled_out(manager.query_id_state["UserTweets"]["qid"]))

            manager._mark_tx_id("UserTweets", "tx", "healthy")
            manager._mark_query_id("UserTweets", "qid", "healthy")
            self.assertFalse(manager._param_ruled_out(manager.tx_id_state["UserTweets"]["tx"]))
            self.assertFalse(manager._param_ruled_out(manager.query_id_state["UserTweets"]["qid"]))

    def test_rate_limit_sleep_cap_is_3600(self):
        manager = APIManager.__new__(APIManager)
        now = int(time.time())
        manager.rate_limits = {"UserTweets": {"remaining": 0, "reset": now + 7200, "limit": 50}}
        manager.retry_policy = lambda: {
            "rate_limit_safety_buffer_seconds": 5,
            "max_rate_limit_sleep_seconds": 3600,
        }

        self.assertEqual(manager.rate_limit_sleep_seconds("UserTweets"), 3600)


if __name__ == "__main__":
    unittest.main()
