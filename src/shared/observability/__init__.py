"""Unified observability: console output, structured events, run reports, coverage inventory."""

from src.shared.observability.coverage_inventory import CoverageInventory, EndpointCoverage
from src.shared.observability.event_recorder import EventRecorder, ObservabilityContext
from src.shared.observability.pipeline_console import PipelineConsole, Verbosity
from src.shared.observability.run_report import RunReportBuilder, endpoint_report_from_result

__all__ = [
    "CoverageInventory",
    "EndpointCoverage",
    "EventRecorder",
    "ObservabilityContext",
    "PipelineConsole",
    "RunReportBuilder",
    "Verbosity",
    "endpoint_report_from_result",
]
