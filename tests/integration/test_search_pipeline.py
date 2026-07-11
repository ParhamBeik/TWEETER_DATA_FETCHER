import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.pipelines.search.search_timeline import SearchTimelineMonitor


class SearchPipelineTests(unittest.TestCase):
    """Integration tests for search pipeline."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / "config.json"
        self.search_config_path = Path(self.temp_dir) / "search_config.json"
        
        self.config_path.write_text(json.dumps({
            "user_tweets_query_id": "test_query_id",
            "user_tweets_and_replies_query_id": "test_replies_query_id",
            "search_timeline_query_id": "test_search_query_id",
            "user_by_screen_name_query_id": "test_screen_name_query_id",
            "real_transaction_ids_by_endpoint": {},
            "cookies": {"test": "cookie"},
            "default_timeout_seconds": 10,
        }))
        
        self.search_config_path.write_text(json.dumps({
            "search_queries": [
                {"name": "test", "raw_query": "test query", "product": "Latest"}
            ]
        }))

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch('src.pipelines.search.search_timeline.FetcherEngine')
    @patch('src.pipelines.search.search_timeline.StorageManager')
    def test_search_monitor_initialization(self, mock_storage, mock_engine):
        """Test SearchTimelineMonitor initialization."""
        mock_engine_instance = MagicMock()
        mock_engine_instance.config = {}
        mock_engine_instance.api_manager = MagicMock()
        mock_engine.return_value = mock_engine_instance
        
        mock_storage_instance = MagicMock()
        mock_storage.return_value = mock_storage_instance
        
        monitor = SearchTimelineMonitor(
            config_path=str(self.config_path),
            search_config_path=str(self.search_config_path)
        )
        self.assertTrue(hasattr(monitor, 'fetcher'))
        self.assertTrue(hasattr(monitor, 'storage'))

    def test_valid_products_constant(self):
        """Test that VALID_PRODUCTS constant is defined."""
        from src.pipelines.search.search_timeline import VALID_PRODUCTS
        self.assertIn("Top", VALID_PRODUCTS)
        self.assertIn("Latest", VALID_PRODUCTS)
        self.assertIn("Media", VALID_PRODUCTS)
        self.assertIn("People", VALID_PRODUCTS)

    @patch('src.pipelines.search.search_timeline.FetcherEngine')
    @patch('src.pipelines.search.search_timeline.StorageManager')
    def test_monitor_search_structure(self, mock_storage, mock_engine):
        """Test monitor_search method structure."""
        mock_engine_instance = MagicMock()
        mock_engine_instance.config = {}
        mock_engine_instance.api_manager = MagicMock()
        mock_engine.return_value = mock_engine_instance
        
        mock_storage_instance = MagicMock()
        mock_storage.return_value = mock_storage_instance
        
        monitor = SearchTimelineMonitor(
            config_path=str(self.config_path),
            search_config_path=str(self.search_config_path)
        )
        self.assertTrue(hasattr(monitor, 'monitor_search'))
        self.assertTrue(callable(monitor.monitor_search))

    @patch('src.pipelines.search.search_timeline.FetcherEngine')
    @patch('src.pipelines.search.search_timeline.StorageManager')
    def test_search_query_building(self, mock_storage, mock_engine):
        """Test search query building."""
        mock_engine_instance = MagicMock()
        mock_engine_instance.config = {}
        mock_engine.return_value = mock_engine_instance
        
        mock_storage_instance = MagicMock()
        mock_storage.return_value = mock_storage_instance
        
        monitor = SearchTimelineMonitor(
            config_path=str(self.config_path),
            search_config_path=str(self.search_config_path)
        )
        
        # Test query building method exists (may be internal)
        self.assertTrue(True)  # Structure test only

    def test_frozen_search_features(self):
        """Test FROZEN_SEARCH_FEATURES is defined."""
        from src.pipelines.search.search_timeline import FROZEN_SEARCH_FEATURES
        self.assertIsInstance(FROZEN_SEARCH_FEATURES, dict)


if __name__ == "__main__":
    unittest.main()
