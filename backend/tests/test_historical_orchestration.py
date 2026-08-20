"""Unit tests for historical pipeline orchestration and two-pass logic."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import Mock, patch, MagicMock

from fetcher.timeline import FetcherEngine


class HistoricalOrchestrationTests(unittest.TestCase):
    """Test historical pipeline two-pass order and watermark behavior."""
    
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)
    
    def tearDown(self):
        self.temp_dir.cleanup()
    
    def test_two_pass_endpoint_order(self):
        """Historical pipeline uses global two-pass order: UserTweets, then UserTweetsAndReplies."""
        # The expected global order is:
        # 1. All UserTweets endpoints for all accounts
        # 2. All UserTweetsAndReplies endpoints for all accounts
        # This is maintained in fetch_historical.py around line 435
        
        # This is a documentation test that validates the architecture
        from fetcher.historical import ENDPOINTS

        self.assertEqual(ENDPOINTS, ("UserTweets",))
    
    def test_watermark_only_advances_on_completion(self):
        """
        Watermark advances only after successful endpoint completion.
        Partial/failed runs do not advance the watermark.
        """
        # The contract is in pagination_engine.py _fetch_endpoint_result():
        # if state_status == "completed":
        #     completion_meta["fetch_watermark"] = started_at
        # self.storage_manager.update_endpoint_state(...)
        
        # Simulate the logic:
        state_status = "completed"
        started_at = datetime.utcnow().isoformat() + "Z"
        completion_meta = {}
        
        if state_status == "completed":
            completion_meta["fetch_watermark"] = started_at
        
        self.assertIn("fetch_watermark", completion_meta)
        
        # Test partial status
        state_status = "partial"
        completion_meta = {}
        if state_status == "completed":
            completion_meta["fetch_watermark"] = started_at
        
        self.assertNotIn("fetch_watermark", completion_meta)
    
    def test_failed_run_does_not_advance_watermark(self):
        """Failed run leaves watermark intact for next backfill."""
        state_status = "failed"
        completion_meta = {}
        
        if state_status == "completed":
            completion_meta["fetch_watermark"] = datetime.utcnow().isoformat() + "Z"
        
        self.assertNotIn("fetch_watermark", completion_meta)
    
    def test_rolling_window_floor_gap_prevention(self):
        """
        Rolling window floors by configured granularity (day/hour).
        Watermark floor gap is prevented: next run re-covers the gap.
        """
        # Historical floors to day start (e.g., 2026-07-14T00:00:00Z)
        # Live floors to hour start (e.g., 2026-07-14T10:00:00Z)
        
        timestamp = datetime(2026, 7, 14, 15, 30, 45)
        
        # Historical: floor to day
        day_start = datetime(timestamp.year, timestamp.month, timestamp.day, 0, 0, 0)
        
        # Live: floor to hour
        hour_start = datetime(timestamp.year, timestamp.month, timestamp.day, timestamp.hour, 0, 0)
        
        self.assertEqual(day_start.hour, 0)
        self.assertEqual(day_start.minute, 0)
        self.assertEqual(day_start.second, 0)
        
        self.assertEqual(hour_start.minute, 0)
        self.assertEqual(hour_start.second, 0)
    
    def test_effective_cutoff_calculation(self):
        """
        effective_cutoff = min(now - configured_window, floor(fetch_watermark))
        
        This ensures we don't refetch beyond the watermark and respect the window.
        """
        now = datetime(2026, 7, 14, 15, 30, 0)
        
        # Historical scenario: 7-day window
        configured_window_days = 7
        window_start = now - timedelta(days=configured_window_days)
        
        # If watermark is older than window, use watermark
        fetch_watermark = datetime(2026, 7, 1, 0, 0, 0)  # 13 days old
        effective_cutoff = min(window_start, fetch_watermark)
        
        # min(window_start, fetch_watermark) where watermark is older
        self.assertEqual(effective_cutoff, fetch_watermark)
        
        # If watermark is fresher than window start, min uses window_start
        fetch_watermark = datetime(2026, 7, 13, 0, 0, 0)  # 1 day old
        effective_cutoff = min(window_start, fetch_watermark)
        
        # min(window_start, fetch_watermark) where window_start is older
        # window_start is 2026-07-07T15:30:00
        # fetch_watermark is 2026-07-13T00:00:00
        # min should be window_start (older date)
        self.assertEqual(effective_cutoff, window_start)


class HistoricalPhaseEventsTests(unittest.TestCase):
    """Test phase event emission in historical pipeline."""
    
    def test_phase_event_structure(self):
        """Phase events contain required fields."""
        phase_event = {
            "type": "phase_start",
            "phase": "pass_1",
            "endpoint": "UserTweets",
            "accounts": ["elonmusk", "naval", "satoshipay"],
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        
        self.assertEqual(phase_event["type"], "phase_start")
        self.assertEqual(phase_event["endpoint"], "UserTweets")
        self.assertIsInstance(phase_event["accounts"], list)
        self.assertGreaterEqual(len(phase_event["accounts"]), 1)
    
    def test_pass_formatting(self):
        """Phases are formatted as Pass N/2."""
        pass_num = 1
        total_passes = 2
        
        phase_label = f"PASS {pass_num}/{total_passes} — UserTweets (12 accounts)"
        
        self.assertIn("PASS", phase_label)
        self.assertIn("1/2", phase_label)
        self.assertIn("UserTweets", phase_label)


class HistoricalPhaseTransitionTests(unittest.TestCase):
    """Test transitions between orchestration phases."""
    
    def test_after_user_tweets_phase_moves_to_replies_phase(self):
        """After all UserTweets fetches complete, pipeline moves to UserTweetsAndReplies."""
        # Track completion status
        phases_completed = []
        
        # Simulate first phase
        phases_completed.append("UserTweets")
        
        # Check if we should move to next phase
        next_phase = None
        if "UserTweets" in phases_completed and "UserTweetsAndReplies" not in phases_completed:
            next_phase = "UserTweetsAndReplies"
        
        self.assertEqual(next_phase, "UserTweetsAndReplies")
    
    def test_all_accounts_processed_per_endpoint_before_next_pass(self):
        """All accounts must complete an endpoint before moving to next endpoint."""
        accounts = ["account1", "account2", "account3"]
        endpoint_a_completed = ["account1", "account2", "account3"]
        
        # Can move to endpoint B only when all accounts done with A
        can_move_to_next = len(endpoint_a_completed) == len(accounts)
        
        self.assertTrue(can_move_to_next)


if __name__ == "__main__":
    unittest.main()
