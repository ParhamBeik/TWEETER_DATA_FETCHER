import unittest
from datetime import datetime, timedelta
from pathlib import Path

from src.pipelines.search.search_timeline import SearchTimelineMonitor


class SearchTimelinePathResolutionTests(unittest.TestCase):
    def setUp(self):
        self.monitor = SearchTimelineMonitor.__new__(SearchTimelineMonitor)
        self.monitor.project_root = Path(__file__).resolve().parents[2]

    def test_resolve_relative_search_config_path(self):
        expected = self.monitor.project_root / "src" / "shared" / "config" / "search_config.json"
        resolved = self.monitor._resolve_path("src/shared/config/search_config.json")
        self.assertEqual(resolved, expected)

    def test_resolve_shared_relative_search_config_path(self):
        expected = self.monitor.project_root / "src" / "shared" / "config" / "search_config.json"
        resolved = self.monitor._resolve_path("shared/config/search_config.json")
        self.assertEqual(resolved, expected)

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


if __name__ == "__main__":
    unittest.main()
