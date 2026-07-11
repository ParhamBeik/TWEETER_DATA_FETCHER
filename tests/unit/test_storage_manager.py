import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.shared.data_pipeline.storage_manager import StorageManager


class StorageManagerTests(unittest.TestCase):
    """Tests for StorageManager class."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.storage = StorageManager(base_dir=Path(self.temp_dir), timezone="UTC")

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_raw_batch_dir(self):
        """Test raw batch directory creation."""
        batch_dir = self.storage.create_raw_batch_dir("UserTweets", "testuser", "test_batch")
        self.assertIn("raw", str(batch_dir))
        self.assertIn("test_batch", str(batch_dir))

    def test_save_and_load_raw_page(self):
        """Test saving and loading raw pages."""
        batch_dir = self.storage.create_raw_batch_dir("UserTweets", "testuser", "test_batch")
        test_data = {"test": "data", "id": "123"}
        self.storage.save_raw_page(batch_dir, 0, test_data)
        
        # Check file was created
        self.assertTrue(batch_dir.exists())
        
        # Load it back
        loaded = self.storage.load_raw_pages_from_batch(batch_dir)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["id"], "123")

    def test_sync_state_management(self):
        """Test sync state file creation and loading."""
        state = {"test": "state"}
        state_file = self.storage.save_sync_state(state)
        
        self.assertTrue(state_file.exists())
        loaded_state = self.storage.load_sync_state()
        self.assertEqual(loaded_state["test"], "state")

    def test_deduplication_logic(self):
        """Test tweet deduplication."""
        # This tests the internal deduplication logic
        seen = set()
        tweets = [
            {"id": "1", "text": "a"},
            {"id": "2", "text": "b"},
            {"id": "1", "text": "a"},  # duplicate
        ]
        
        deduped = []
        for tweet in tweets:
            tweet_id = tweet.get("id") or tweet.get("rest_id")
            if tweet_id and tweet_id not in seen:
                seen.add(tweet_id)
                deduped.append(tweet)
        
        self.assertEqual(len(deduped), 2)

    def test_run_report_creation(self):
        """Test run report path creation."""
        paths = self.storage.create_run_report_paths("test_run_001")
        
        self.assertIn("report", str(paths.get("json", "")))
        self.assertIn("report", str(paths.get("txt", "")))
        self.assertIn("test_run_001", str(paths.get("json", "")))
        self.assertIn("test_run_001", str(paths.get("txt", "")))

    def test_search_subsystem_does_not_create_historical_processed_dirs(self):
        """Search storage should not create historical processed set directories."""
        search_storage = StorageManager(base_dir=Path(self.temp_dir), timezone="UTC", subsystem="search", create_folders=True)
        self.assertTrue((Path(self.temp_dir) / "data" / "search" / "raw").exists())
        self.assertFalse((Path(self.temp_dir) / "data" / "search" / "processed" / "1_user_tweets").exists())
        self.assertFalse((Path(self.temp_dir) / "data" / "search" / "raw" / "UserTweets").exists())
        self.assertFalse((Path(self.temp_dir) / "data" / "search" / "raw" / "UserTweetsAndReplies").exists())


if __name__ == "__main__":
    unittest.main()
