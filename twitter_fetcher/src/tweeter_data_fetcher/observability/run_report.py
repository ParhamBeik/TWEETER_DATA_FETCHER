#!/usr/bin/env python3
"""Canonical run report schema shared across historical, live, and search pipelines."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional


def endpoint_report_from_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a FetcherEngine endpoint result into report fields."""
    status = result.get("status")
    transport = result.get("transport")
    endpoint_status = (
        "verified_browser_fallback"
        if status == "completed" and transport == "browser_fallback"
        else ("verified_http" if status == "completed" else ("unverified" if status in {"partial", "skipped"} else "failed"))
    )
    return {
        "status": status,
        "endpoint_status": endpoint_status,
        "outcome": result.get("outcome"),
        "reason": result.get("reason"),
        "pages_fetched": result.get("pages_fetched", 0),
        "raw_batch_path": result.get("raw_batch_path"),
        "last_cursor": result.get("last_cursor"),
        "cursor_termination_reason": result.get("cursor_termination_reason"),
        "cursor_history": result.get("cursor_history", []),
        "last_http_status": result.get("last_http_status"),
        "attempts": result.get("attempts", 0),
        "error_samples": result.get("error_samples", []),
        "transport": transport,
        "bootstrap_route": result.get("bootstrap_route"),
        "rate_headers": result.get("rate_headers"),
        "output_paths": result.get("output_paths"),
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "window_coverage": result.get("window_coverage"),
    }


class RunReportBuilder:
    """Build a canonical run report dict."""

    def __init__(self, *, subsystem: str, run_id: str) -> None:
        self.report: Dict[str, Any] = {
            "run_id": run_id,
            "subsystem": subsystem,
            "started_at": datetime.utcnow().isoformat() + "Z",
            "finished_at": None,
            "phases": [],
            "accounts": {},
            "searches": {},
            "summary": {},
        }

    def start_phase(
        self,
        name: str,
        *,
        endpoint: Optional[str] = None,
        accounts: Optional[List[str]] = None,
        pass_index: Optional[int] = None,
        pass_total: Optional[int] = None,
    ) -> Dict[str, Any]:
        phase = {
            "name": name,
            "endpoint": endpoint,
            "accounts": list(accounts or []),
            "pass_index": pass_index,
            "pass_total": pass_total,
            "started_at": datetime.utcnow().isoformat() + "Z",
            "finished_at": None,
        }
        self.report["phases"].append(phase)
        return phase

    def finish_phase(self, phase: Dict[str, Any]) -> None:
        phase["finished_at"] = datetime.utcnow().isoformat() + "Z"

    def ensure_account(self, username: str) -> Dict[str, Any]:
        accounts = self.report.setdefault("accounts", {})
        if username not in accounts:
            accounts[username] = {"endpoints": {}}
        return accounts[username]

    def set_endpoint(self, username: str, endpoint: str, result: Dict[str, Any], **extra: Any) -> None:
        account = self.ensure_account(username)
        report = endpoint_report_from_result(result)
        report.update(extra)
        account.setdefault("endpoints", {})[endpoint] = report

    def update_summary(self) -> None:
        summary = {
            "successful_endpoints": 0,
            "partial_endpoints": 0,
            "failed_endpoints": 0,
            "skipped_endpoints": 0,
            "txt_unverified_endpoints": 0,
        }
        for account_report in self.report.get("accounts", {}).values():
            for endpoint_report in (account_report.get("endpoints", {}) or {}).values():
                status = endpoint_report.get("status")
                if status == "completed":
                    summary["successful_endpoints"] += 1
                elif status == "partial":
                    summary["partial_endpoints"] += 1
                elif status == "failed":
                    summary["failed_endpoints"] += 1
                elif status == "skipped":
                    summary["skipped_endpoints"] += 1
                if endpoint_report.get("processed_txt_verified") is False:
                    summary["txt_unverified_endpoints"] += 1
        self.report["summary"] = summary

    def finish(self) -> Dict[str, Any]:
        self.report["finished_at"] = datetime.utcnow().isoformat() + "Z"
        self.update_summary()
        return self.report

    def build(self) -> Dict[str, Any]:
        return self.report
