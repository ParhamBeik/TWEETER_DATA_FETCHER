import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tweeter_data_fetcher.pipelines.search.service import SearchTimelineMonitor


class SearchTimelinePathResolutionTests(unittest.TestCase):
    def setUp(self):
        self.monitor = SearchTimelineMonitor.__new__(SearchTimelineMonitor)
        self.monitor.project_root = Path(__file__).resolve().parents[2]

    def test_resolve_relative_search_config_path(self):
        expected = self.monitor.project_root / "config" / "searches.json"
        resolved = self.monitor._resolve_path("config/searches.json")
        self.assertEqual(resolved, expected)

    def test_resolve_absolute_search_config_path(self):
        abs_path = self.monitor.project_root / "config" / "searches.json"
        resolved = self.monitor._resolve_path(str(abs_path))
        self.assertEqual(resolved, abs_path)

    def test_window_crossed_page_does_not_stop_pagination(self):
        window_start = datetime.utcnow() - timedelta(hours=6)
        page_result = {
            "tweets": [{"raw_timestamp": "Wed Jan 01 00:00:00 +0000 2020"}],
            "next_cursor": "cursor-2",
        }
        stop, reason = self.monitor.should_stop_search_pagination(
            page_result=page_result,
            window_start=window_start,
            cursor="cursor-1",
            cursor_history={"cursor-1"},
        )
        self.assertFalse(stop)
        self.assertIsNone(reason)

    def test_policy_uses_pagination_depth_when_cap_missing(self):
        self.monitor.config = {"api_config": {"pagination_safety_cap_pages": 50}}
        policy = self.monitor._policy_for_search({"pagination_depth": 3})
        self.assertEqual(policy["pagination_safety_cap_pages"], 3)


if __name__ == "__main__":
    unittest.main()
