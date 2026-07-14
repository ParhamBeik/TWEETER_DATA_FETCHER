#!/usr/bin/env python3
"""Structured NDJSON event log and error detail files."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class ObservabilityContext:
    """Bundle console + recorder for injection into fetch/auth layers."""

    console: Any
    recorder: "EventRecorder"
    subsystem: str = "historical_live"


class EventRecorder:
    """Append structured events to logs/events.jsonl and error detail files."""

    def __init__(self, logs_dir: Path, *, subsystem: str = "historical_live") -> None:
        self.logs_dir = Path(logs_dir)
        self.subsystem = subsystem
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.events_file = self.logs_dir / "events.jsonl"
        self.errors_dir = self.logs_dir / "errors"
        self.errors_dir.mkdir(parents=True, exist_ok=True)
        self.summary_file = self.logs_dir / "http_summary.json"

    def emit(self, event_type: str, **fields: Any) -> None:
        payload = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "type": event_type,
            "subsystem": self.subsystem,
            **fields,
        }
        try:
            with self.events_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def emit_page_fetched(
        self,
        *,
        account: str,
        endpoint: str,
        page: int,
        cursor_in: Optional[str],
        cursor_out: Optional[str],
        http_status: int,
        items: int,
    ) -> None:
        self.emit(
            "page_fetched",
            account=account,
            endpoint=endpoint,
            page=page,
            cursor_in=cursor_in,
            cursor_out=cursor_out,
            http_status=http_status,
            items=items,
        )

    def emit_phase_start(
        self,
        *,
        phase: str,
        endpoint: Optional[str] = None,
        accounts: Optional[int] = None,
        pass_index: Optional[int] = None,
        pass_total: Optional[int] = None,
    ) -> None:
        self.emit(
            "phase_start",
            phase=phase,
            endpoint=endpoint,
            accounts=accounts,
            pass_index=pass_index,
            pass_total=pass_total,
        )

    def emit_http_error(
        self,
        *,
        account: str,
        endpoint: str,
        status_code: int,
        cursor: Optional[str],
        request_url: str,
        request_headers: Dict[str, Any],
        variables: Dict[str, Any],
        response_text: str,
        title: str = "http_error",
    ) -> str:
        safe_account = re.sub(r"[^a-zA-Z0-9_\\-]+", "_", account or "unknown")
        safe_endpoint = re.sub(r"[^a-zA-Z0-9_\\-]+", "_", endpoint or "unknown")
        stamp = datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")
        detail_name = f"{stamp}_{safe_account}_{safe_endpoint}_{status_code}.json"
        detail_path = self.errors_dir / detail_name
        block = {
            "title": title,
            "status_code": int(status_code),
            "account": account,
            "endpoint": endpoint,
            "cursor": cursor,
            "request_url": request_url,
            "headers": request_headers,
            "variables": variables,
            "response_text": (response_text or "")[:8000],
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        try:
            with detail_path.open("w", encoding="utf-8") as handle:
                json.dump(block, handle, ensure_ascii=False, indent=2)
        except Exception:
            detail_path = self.errors_dir / "unknown_error.json"

        detail_ref = str(detail_path)
        self.emit(
            "http_error",
            account=account,
            endpoint=endpoint,
            status=status_code,
            cursor=cursor,
            detail_ref=detail_ref,
        )
        self._increment_summary(account, endpoint, status_code)
        return detail_ref

    def emit_auto_refresh_start(self, *, trigger: str, endpoint: str, username: Optional[str] = None) -> None:
        self.emit(
            "auto_refresh_start",
            trigger=trigger,
            endpoint=endpoint,
            username=username,
        )

    def emit_auto_refresh_done(
        self,
        *,
        endpoint: str,
        updated: List[str],
        success: bool,
        username: Optional[str] = None,
    ) -> None:
        self.emit(
            "auto_refresh_done",
            endpoint=endpoint,
            updated=updated,
            success=success,
            username=username,
        )

    def emit_auto_refresh_param_updated(self, *, key: str, value_preview: str) -> None:
        self.emit("auto_refresh_param_updated", key=key, value_preview=value_preview[:40])

    def _increment_summary(self, account: str, endpoint: str, status_code: int) -> None:
        summary: Dict[str, Any] = {}
        if self.summary_file.exists():
            try:
                with self.summary_file.open("r", encoding="utf-8") as handle:
                    summary = json.load(handle)
            except Exception:
                summary = {}
        if not isinstance(summary, dict):
            summary = {}
        by_account = summary.setdefault("by_account", {})
        by_endpoint = summary.setdefault("by_endpoint", {})
        by_status = summary.setdefault("by_status_code", {})
        acct_key = account or "unknown"
        by_account[acct_key] = int(by_account.get(acct_key, 0)) + 1
        by_endpoint[endpoint] = int(by_endpoint.get(endpoint, 0)) + 1
        status_key = str(status_code)
        by_status[status_key] = int(by_status.get(status_key, 0)) + 1
        try:
            with self.summary_file.open("w", encoding="utf-8") as handle:
                json.dump(summary, handle, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # Backward-compatible alias for migrated 404 metric path
    def emit_404_metric(self, event: Dict[str, Any]) -> None:
        detail_ref = self.emit_http_error(
            account=str(event.get("account", "")),
            endpoint=str(event.get("endpoint", "")),
            status_code=int(event.get("status_code", 404)),
            cursor=event.get("cursor"),
            request_url=str(event.get("request_url", "")),
            request_headers=event.get("headers") or {},
            variables=event.get("variables") or {},
            response_text=str(event.get("response_text", "")),
            title="404_metric",
        )
        self.emit("404_metric", detail_ref=detail_ref, **{k: v for k, v in event.items() if k != "response_text"})
