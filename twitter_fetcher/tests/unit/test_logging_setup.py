"""Unit tests for the centralized logging setup (logging_setup.py)."""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from tweeter_data_fetcher.observability.event_recorder import EventRecorder
from tweeter_data_fetcher.observability.logging_setup import (
    ROOT_LOGGER_NAME,
    attach_run_id,
    configure_logging,
    reset_logging,
)
from tweeter_data_fetcher.observability.pipeline_console import PipelineConsole, Verbosity


class LoggingSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_logging()
        self._tmp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self._tmp.name) / "logs"

    def tearDown(self) -> None:
        reset_logging()
        self._tmp.cleanup()

    def _log_text(self) -> str:
        # The file handler writes the latest subsystem file; find it.
        files = list(self.logs_dir.glob("*.log"))
        self.assertTrue(files, "no log file was written")
        return files[0].read_text(encoding="utf-8")

    def test_configure_logging_writes_file_handler(self) -> None:
        configure_logging(subsystem="historical", logs_dir=self.logs_dir)
        logging.getLogger(f"{ROOT_LOGGER_NAME}.twitter.client").warning("boom")
        text = self._log_text()
        self.assertIn("boom", text)
        self.assertIn("[WARNING]", text)

    def test_run_id_is_stamped_on_records(self) -> None:
        configure_logging(subsystem="live", logs_dir=self.logs_dir)
        attach_run_id("run_abc123")
        logging.getLogger(f"{ROOT_LOGGER_NAME}.x").info("hello")
        self.assertIn("run=run_abc123", self._log_text())

    def test_attach_run_id_is_noop_before_configure(self) -> None:
        # Should not raise even though logging was never configured.
        attach_run_id("run_xyz")

    def test_console_propagates_to_file(self) -> None:
        configure_logging(subsystem="search", logs_dir=self.logs_dir)
        PipelineConsole(subsystem="search").info("page fetched")
        self.assertIn("page fetched", self._log_text())

    def test_configure_logging_is_idempotent(self) -> None:
        configure_logging(subsystem="historical", logs_dir=self.logs_dir)
        before = len(logging.getLogger(ROOT_LOGGER_NAME).handlers)
        configure_logging(subsystem="historical", logs_dir=self.logs_dir)
        after = len(logging.getLogger(ROOT_LOGGER_NAME).handlers)
        self.assertEqual(before, after)

    def test_verbosity_maps_to_stderr_level(self) -> None:
        logger = configure_logging(
            subsystem="historical", logs_dir=self.logs_dir, verbosity=Verbosity.QUIET
        )
        stderr_handlers = [
            h for h in logger.handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        self.assertTrue(stderr_handlers)
        self.assertEqual(stderr_handlers[0].level, logging.WARNING)


class EventRecorderRunIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self._tmp.name) / "logs"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_run_id_in_payload(self) -> None:
        recorder = EventRecorder(self.logs_dir, subsystem="historical_live")
        recorder.emit("page_fetched", account="a", endpoint="e")
        line = (self.logs_dir / "events.jsonl").read_text(encoding="utf-8").strip()
        self.assertIn('"run_id": "-"', line)

    def test_custom_run_id_in_payload(self) -> None:
        recorder = EventRecorder(self.logs_dir, subsystem="historical_live", run_id="run_42")
        recorder.emit("phase_start", phase="p1")
        line = (self.logs_dir / "events.jsonl").read_text(encoding="utf-8").strip()
        self.assertIn('"run_id": "run_42"', line)

    def test_cycle_lifecycle_fields_are_preserved(self) -> None:
        recorder = EventRecorder(self.logs_dir, subsystem="search", run_id="run_42")
        recorder.emit("cycle_end", searches_fetched=2, statuses=["completed", "partial"])
        payload = json.loads((self.logs_dir / "events.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(payload["type"], "cycle_end")
        self.assertEqual(payload["searches_fetched"], 2)


if __name__ == "__main__":
    unittest.main()
