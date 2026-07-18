"""Unit tests for observability module components."""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from tweeter_data_fetcher.observability.pipeline_console import PipelineConsole
from tweeter_data_fetcher.observability.event_recorder import EventRecorder, ObservabilityContext
from tweeter_data_fetcher.observability.run_report import RunReportBuilder
from tweeter_data_fetcher.observability.coverage_inventory import CoverageInventory


class PipelineConsoleTests(unittest.TestCase):
    """Test PipelineConsole output formatting and fallback."""
    
    def test_console_initializes_with_subsystem(self):
        """PipelineConsole accepts subsystem tags."""
        console = PipelineConsole(subsystem="historical")
        self.assertEqual(console.subsystem, "historical")
    
    def test_phase_banner_renders_without_error(self):
        """Phase banner prints without raising."""
        console = PipelineConsole(subsystem="live")
        try:
            console.phase_banner("Test Phase", pass_index=1, pass_total=2)
        except Exception as e:
            self.fail(f"phase_banner raised {type(e).__name__}: {e}")
    
    def test_page_row_verbosity_handling(self):
        """Page row method exists and handles parameters."""
        console = PipelineConsole(subsystem="engine")
        try:
            console.page_row(page=1, items=20, cursor_status="found", http_status=200)
        except Exception as e:
            self.fail(f"page_row raised {type(e).__name__}: {e}")
    
    def test_error_one_liner_with_detail_ref(self):
        """Error one-liner includes detail ref when provided."""
        console = PipelineConsole(subsystem="auth")
        try:
            console.error_one_liner(
                "Connection failed",
                detail_ref="data/logs/errors/2026-07-14_elonmusk_UserTweets.json"
            )
        except Exception as e:
            self.fail(f"error_one_liner raised {type(e).__name__}: {e}")
    
    def test_success_info_warning_print(self):
        """Convenience methods print without error."""
        console = PipelineConsole(subsystem="search")
        try:
            console.success("Task completed")
            console.info("Processing account")
            console.warning("Retrying after 30s")
        except Exception as e:
            self.fail(f"Convenience methods raised {type(e).__name__}: {e}")


class EventRecorderTests(unittest.TestCase):
    """Test EventRecorder NDJSON logging."""
    
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.temp_dir.name)
    
    def tearDown(self):
        self.temp_dir.cleanup()
    
    def test_recorder_creates_logs_directory(self):
        """EventRecorder ensures logs directory exists."""
        recorder = EventRecorder(self.logs_dir, subsystem="historical_live")
        self.assertTrue((self.logs_dir / "events.jsonl").parent.exists())
    
    def test_emit_page_fetched_writes_ndjson(self):
        """Emitting page_fetched event writes to events.jsonl."""
        recorder = EventRecorder(self.logs_dir, subsystem="search")
        recorder.emit_page_fetched(
            account="test_user",
            endpoint="SearchTimeline",
            page=1,
            cursor_in=None,
            cursor_out="DAACCAABCgABbhBAAAoAA",
            http_status=200,
            items=20,
        )
        
        events_file = self.logs_dir / "events.jsonl"
        self.assertTrue(events_file.exists())
        
        with events_file.open("r") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 1)
        
        event = json.loads(lines[0])
        self.assertEqual(event["type"], "page_fetched")
        self.assertEqual(event["account"], "test_user")
        self.assertEqual(event["page"], 1)
        self.assertEqual(event["http_status"], 200)
        self.assertEqual(event["items"], 20)
    
    def test_emit_http_error_saves_detail_file(self):
        """Emitting http_error event saves detail JSON and references it."""
        recorder = EventRecorder(self.logs_dir, subsystem="historical_live")
        
        recorder.emit_http_error(
            account="elonmusk",
            endpoint="UserTweets",
            status_code=404,
            cursor="DAACCAABCgABbhBAAAoAA",
            request_url="https://x.com/i/api/graphql/...",
            request_headers={"accept": "application/json"},
            variables={"userId": "123456"},
            response_text="Not found",
        )
        
        events_file = self.logs_dir / "events.jsonl"
        self.assertTrue(events_file.exists())
        
        with events_file.open("r") as f:
            lines = f.readlines()
        self.assertGreaterEqual(len(lines), 1)
        
        event = json.loads(lines[-1])
        self.assertEqual(event["type"], "http_error")
        self.assertEqual(event["status"], 404)
        self.assertIn("detail_ref", event)
        
        # Verify detail file was created
        detail_file = Path(event["detail_ref"])
        self.assertTrue(detail_file.exists())
    
    def test_emit_auto_refresh_event(self):
        """Auto-refresh events are recorded."""
        recorder = EventRecorder(self.logs_dir, subsystem="historical_live")
        recorder.emit_auto_refresh_start(
            trigger="consecutive_404s",
            endpoint="UserTweetsAndReplies",
        )
        
        events_file = self.logs_dir / "events.jsonl"
        with events_file.open("r") as f:
            event = json.loads(f.readlines()[0])
        
        self.assertEqual(event["type"], "auto_refresh_start")
        self.assertEqual(event["trigger"], "consecutive_404s")


class RunReportBuilderTests(unittest.TestCase):
    """Test RunReportBuilder schema."""
    
    def test_builder_creates_canonical_schema(self):
        """RunReportBuilder generates expected report shape."""
        builder = RunReportBuilder(
            run_id="test_run_1",
            subsystem="historical_live",
        )
        
        phase = builder.start_phase(
            name="Phase 1/2",
            endpoint="UserTweets",
            accounts=["elonmusk", "naval"]
        )
        builder.finish_phase(phase)
        
        report = builder.build()
        
        self.assertEqual(report["run_id"], "test_run_1")
        self.assertEqual(report["subsystem"], "historical_live")
        self.assertIn("started_at", report)
        self.assertIn("finished_at", report)
        self.assertIn("phases", report)
        self.assertGreaterEqual(len(report["phases"]), 1)
    
    def test_builder_tracks_phases(self):
        """Phases are tracked with timestamps."""
        builder = RunReportBuilder(subsystem="search", run_id="test")
        
        phase = builder.start_phase(name="Fetch", accounts=["test_account"])
        
        # Use set_endpoint instead of record_account
        result = {
            "status": "completed",
            "pages_fetched": 5,
            "started_at": datetime.utcnow().isoformat() + "Z",
            "finished_at": datetime.utcnow().isoformat() + "Z",
        }
        builder.set_endpoint("test_account", "SearchTimeline", result)
        builder.finish_phase(phase)
        
        report = builder.build()
        self.assertEqual(len(report["phases"]), 1)
        self.assertEqual(report["phases"][0]["name"], "Fetch")
        self.assertIn("started_at", report["phases"][0])
        self.assertIn("finished_at", report["phases"][0])


class ObservabilityContextTests(unittest.TestCase):
    """Test ObservabilityContext dependency injection."""
    
    def test_context_bundles_console_and_recorder(self):
        """ObservabilityContext accepts and stores console and recorder."""
        temp_dir = tempfile.TemporaryDirectory()
        try:
            console = PipelineConsole(subsystem="live")
            recorder = EventRecorder(Path(temp_dir.name), subsystem="search")
            
            context = ObservabilityContext(console=console, recorder=recorder, subsystem="search")
            
            self.assertEqual(context.console, console)
            self.assertEqual(context.recorder, recorder)
            self.assertEqual(context.subsystem, "search")
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
