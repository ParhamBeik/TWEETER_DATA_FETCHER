#!/usr/bin/env python3
"""Structured NDJSON event log and error detail files for all pipelines."""

from __future__ import annotations

import json
import logging
import re
from hashlib import sha256
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)


def _fingerprint(value: Optional[str]) -> Optional[str]:
    return sha256(str(value).encode("utf-8")).hexdigest()[:12] if value else None


def _safe_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    hidden = {"authorization", "cookie", "x-csrf-token", "x-client-transaction-id"}
    return {key: "[redacted]" if key.lower() in hidden else value for key, value in headers.items()}


def _safe_variables(variables: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: _fingerprint(value) if key.lower() == "cursor" else value
        for key, value in variables.items()
    }


def redact_exception(exc: Any, limit: int = 500) -> str:
    """Keep the useful exception class/cause while removing URL query data."""
    text = re.sub(r"((?:https?://|url: /)[^?\s]+)\?\S+", r"\1?[redacted]", str(exc))
    return text[:limit]


@dataclass
class ObservabilityContext:
    """Bundle console + recorder for injection into fetch/auth layers."""

    console: Any
    recorder: "EventRecorder"
    subsystem: str = "historical_live"
    run_id: str = "-"


class EventRecorder:
    """Append structured events to logs/events.jsonl and error detail files."""

    def __init__(
        self,
        logs_dir: Path,
        *,
        subsystem: str = "historical_live",
        run_id: str = "-",
    ) -> None:
        self.logs_dir = Path(logs_dir)
        self.subsystem = subsystem
        self.run_id = run_id
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
            "run_id": self.run_id,
            **fields,
        }
        try:
            with self.events_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("Failed to append event type=%s to %s", event_type, self.events_file)

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
        transport: str = "http",
        latency_ms: Optional[int] = None,
        attempt: int = 1,
        recovery: Optional[str] = None,
    ) -> None:
        self.emit(
            "page_fetched",
            account=account,
            endpoint=endpoint,
            page=page,
            cursor_in=_fingerprint(cursor_in),
            cursor_out=_fingerprint(cursor_out),
            http_status=http_status,
            items=items,
            transport=transport,
            latency_ms=latency_ms,
            attempt=attempt,
            recovery=recovery,
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
        stamp = datetime.utcnow().strftime("%Y-%m-%d_%H%M%S_%f")
        detail_name = f"{stamp}_{safe_account}_{safe_endpoint}_{status_code}.json"
        detail_path = self.errors_dir / detail_name
        url = urlsplit(request_url)
        block = {
            "title": title,
            "status_code": int(status_code),
            "account": account,
            "endpoint": endpoint,
            "cursor": _fingerprint(cursor),
            "request_url": urlunsplit((url.scheme, url.netloc, url.path, "", "")),
            "headers": _safe_headers(request_headers),
            "variables": _safe_variables(variables),
            "response_text": (response_text or "")[:8000],
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        try:
            with detail_path.open("w", encoding="utf-8") as handle:
                json.dump(block, handle, ensure_ascii=False, indent=2)
        except OSError:
            logger.exception("Failed to write HTTP error detail to %s", detail_path)
            detail_path = self.errors_dir / "unknown_error.json"

        detail_ref = str(detail_path)
        self.emit(
            "http_error",
            account=account,
            endpoint=endpoint,
            status=status_code,
            cursor=_fingerprint(cursor),
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
            except (OSError, json.JSONDecodeError):
                logger.exception("Failed to read HTTP summary from %s", self.summary_file)
                summary = {}
        if not isinstance(summary, dict):
            summary = {}
        by_account = summary.setdefault("by_account", {})
        by_endpoint = summary.setdefault("by_endpoint", {})
        by_status = summary.setdefault("by_status_code", {})
        ledger = summary.setdefault("failure_ledger", {})
        summary["run_id"] = self.run_id
        acct_key = account or "unknown"
        by_account[acct_key] = int(by_account.get(acct_key, 0)) + 1
        by_endpoint[endpoint] = int(by_endpoint.get(endpoint, 0)) + 1
        status_key = str(status_code)
        by_status[status_key] = int(by_status.get(status_key, 0)) + 1
        signature = f"{endpoint}:{status_key}"
        now = datetime.utcnow().isoformat() + "Z"
        failure = ledger.setdefault(
            signature,
            {
                "count": 0,
                "targets": [],
                "first_seen": now,
                "last_seen": now,
                "recovered": False,
                "final_disposition": "unrecovered",
            },
        )
        failure["count"] = int(failure.get("count", 0)) + 1
        failure["last_seen"] = now
        if acct_key not in failure["targets"]:
            failure["targets"].append(acct_key)
        try:
            with self.summary_file.open("w", encoding="utf-8") as handle:
                json.dump(summary, handle, ensure_ascii=False, indent=2)
        except OSError:
            logger.exception("Failed to write HTTP summary to %s", self.summary_file)

    def mark_http_recovered(self, account: str, endpoint: str) -> None:
        try:
            summary = json.loads(self.summary_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        changed = False
        now = datetime.utcnow().isoformat() + "Z"
        for signature, failure in summary.get("failure_ledger", {}).items():
            if signature.startswith(f"{endpoint}:") and account in failure.get("targets", []):
                failure.update({"recovered": True, "recovered_at": now, "final_disposition": "recovered"})
                changed = True
        if changed:
            try:
                self.summary_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                logger.exception("Failed to mark HTTP recovery in %s", self.summary_file)
