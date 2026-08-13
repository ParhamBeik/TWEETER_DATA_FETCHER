import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]


class CanonicalImportTests(unittest.TestCase):
    def test_compatibility_symbols_are_reexported(self):
        from tweeter_data_fetcher.pipelines.live.service import LiveMonitor
        from tweeter_data_fetcher.pipelines.search.service import SearchTimelineMonitor
        from tweeter_data_fetcher.storage.facade import StorageManager
        from tweeter_data_fetcher.processing.core import RollingWindowEvaluator, TweetSetProcessor
        from tweeter_data_fetcher.x_api.client import APIManager
        from tweeter_data_fetcher.x_api.timeline import FetcherEngine

        self.assertEqual(LiveMonitor.__name__, "LiveMonitor")
        self.assertEqual(SearchTimelineMonitor.__name__, "SearchTimelineMonitor")
        self.assertEqual(StorageManager.__name__, "StorageManager")
        self.assertEqual(TweetSetProcessor.__name__, "TweetSetProcessor")
        self.assertEqual(RollingWindowEvaluator.__name__, "RollingWindowEvaluator")
        self.assertEqual(APIManager.__name__, "APIManager")
        self.assertEqual(FetcherEngine.__name__, "FetcherEngine")


class ConfigurationResolutionTests(unittest.TestCase):
    def test_resolution_precedence(self):
        from tweeter_data_fetcher.configuration import resolve_config_path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explicit = root / "explicit.json"
            env = root / "env.json"
            canonical = root / "config" / "config.json"
            for path in (explicit, env, canonical):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")

            with patch.dict(os.environ, {"TDF_CONFIG": str(env)}):
                self.assertEqual(resolve_config_path(explicit, project_root=root), explicit.resolve())
                self.assertEqual(resolve_config_path(project_root=root), env.resolve())
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(resolve_config_path(project_root=root), canonical.resolve())
                canonical.unlink()
                # No legacy fallback: the canonical default is returned even when missing.
                self.assertEqual(resolve_config_path(project_root=root), canonical.resolve())

    def test_account_and_search_files_are_valid_json(self):
        accounts = json.loads((REPO_ROOT / "config" / "accounts.json").read_text())
        searches = json.loads((REPO_ROOT / "config" / "searches.json").read_text())
        self.assertIsInstance(accounts, dict)
        self.assertIsInstance(searches, list)


class EntrypointCompatibilityTests(unittest.TestCase):
    def test_runnable_modules_expose_existing_flags(self):
        cases = {
            "tweeter_data_fetcher.pipelines.historical.service": "--only",
            "tweeter_data_fetcher.pipelines.live.service": "--account",
            "tweeter_data_fetcher.pipelines.search.service": "--once",
            "tweeter_data_fetcher.observability.coverage_inventory": "--format",
            "tweeter_data_fetcher.x_api.auth": "--interactive",
        }
        for module, flag in cases.items():
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-m", module, "--help"],
                    cwd=REPO_ROOT,
                    env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(flag, result.stdout)


class RequestStateTests(unittest.TestCase):
    def test_request_state_store_preserves_health_and_limits(self):
        from tweeter_data_fetcher.x_api.request_state import RequestStateStore

        with tempfile.TemporaryDirectory() as tmp:
            store = RequestStateStore(Path(tmp), failure_limit=3)
            store.mark_parameter("tx_id_state.json", "UserTweets", "tx", healthy=False)
            store.mark_parameter("tx_id_state.json", "UserTweets", "tx", healthy=False)
            state = store.mark_parameter("tx_id_state.json", "UserTweets", "tx", healthy=False)
            store.save("rate_limits.json", {"UserTweets": {"remaining": 0, "reset": 123}})

            self.assertEqual(state, {"status": "stale", "failures": 3})
            self.assertEqual(store.load("rate_limits.json", {})["UserTweets"]["reset"], 123)


if __name__ == "__main__":
    unittest.main()
