#!/usr/bin/env python3
"""Observability: Rich terminal console, rotating file logs, NDJSON events.

The console owns the terminal and forwards every message to the package
logger, so a line printed to the operator is always also on disk.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
except Exception:  # pragma: no cover
    Console = None
    Panel = None
    Table = None


SEP = "═" * 72
SUBSYSTEM_TAGS = {
    "historical": "HIST",
    "live": "LIVE",
    "search": "SEARCH",
    "engine": "ENGINE",
    "auth": "AUTH",
}


class Verbosity(str, Enum):
    QUIET = "quiet"
    NORMAL = "normal"
    VERBOSE = "verbose"


class PipelineConsole:
    """Unified Rich console with plain-text fallback."""

    def __init__(
        self,
        subsystem: str = "engine",
        *,
        verbosity: Verbosity = Verbosity.NORMAL,
    ) -> None:
        self.subsystem = str(subsystem or "engine").strip().lower()
        self.tag = SUBSYSTEM_TAGS.get(self.subsystem, self.subsystem.upper()[:6])
        self.verbosity = verbosity
        self.rich_enabled = Console is not None
        self._console = Console() if self.rich_enabled else None
        # Child of the package root logger. Propagates to the root file handler
        # (so every console line is captured on disk) but is dropped from the
        # stderr tail by logging_setup._NoConsoleOnStderr (Rich owns terminal).
        self._logger = logging.getLogger(f"fetcher.console.{self.subsystem}")

    def _prefix(self) -> str:
        return f"[{self.tag}]"

    def info(self, message: str) -> None:
        self._logger.info(message)
        if self.rich_enabled:
            self._console.print(f"[bold cyan]{self._prefix()}[/bold cyan] {message}")
        else:
            print(f"{self._prefix()} {message}", flush=True)

    def success(self, message: str) -> None:
        self._logger.info("OK %s", message)
        if self.rich_enabled:
            self._console.print(f"[bold green]{self._prefix()} ✓[/bold green] {message}")
        else:
            print(f"{self._prefix()} ✓ {message}", flush=True)

    def warning(self, message: str) -> None:
        self._logger.warning(message)
        if self.rich_enabled:
            self._console.print(f"[bold yellow]{self._prefix()} ⚠[/bold yellow] {message}")
        else:
            print(f"{self._prefix()} ⚠ {message}", flush=True)

    def error(self, message: str) -> None:
        self._logger.error(message)
        if self.rich_enabled:
            self._console.print(f"[bold red]{self._prefix()} ✗[/bold red] {message}")
        else:
            print(f"{self._prefix()} ✗ {message}", flush=True)

    def error_one_liner(self, message: str, detail_ref: Optional[str] = None) -> None:
        suffix = f" → see {detail_ref}" if detail_ref else ""
        self.error(f"{message}{suffix}")

    def banner(self, title: str, *, subtitle: Optional[str] = None) -> None:
        self._logger.info("banner: %s", title)
        body = subtitle or title
        if self.rich_enabled and Panel is not None:
            self._console.print(
                Panel.fit(body, title=f"{self._prefix()} {title}", border_style="magenta")
            )
        else:
            print(SEP)
            print(f"{self._prefix()} {title}")
            if subtitle and subtitle != title:
                print(subtitle)
            print(SEP)

    def phase_banner(
        self,
        phase_name: str,
        *,
        pass_index: Optional[int] = None,
        pass_total: Optional[int] = None,
        endpoint: Optional[str] = None,
        account_count: Optional[int] = None,
    ) -> None:
        parts = [phase_name]
        if pass_index is not None and pass_total is not None:
            parts.insert(0, f"PASS {pass_index}/{pass_total}")
        if endpoint:
            parts.append(f"— {endpoint}")
        if account_count is not None:
            parts.append(f"({account_count} accounts)")
        title = " ".join(parts)
        self.banner(title)

    def page_row(
        self,
        *,
        page: int,
        items: int,
        cursor_status: str,
        http_status: Optional[int] = None,
        next_page: Optional[int] = None,
        account: Optional[str] = None,
        endpoint: Optional[str] = None,
        transport: str = "http",
        latency_ms: Optional[int] = None,
        attempt: int = 1,
        recovery: Optional[str] = None,
    ) -> None:
        if self.verbosity == Verbosity.QUIET:
            return
        target = f"@{account} {endpoint}  " if account and endpoint else ""
        http_part = f"  http={http_status}" if http_status is not None else ""
        latency_part = f"  latency={latency_ms}ms" if latency_ms is not None else ""
        attempt_part = f"  attempt={attempt}" if attempt > 1 else ""
        recovery_part = f"  recovery={recovery}" if recovery else ""
        arrow = f" → page {next_page}" if next_page else ""
        self.info(
            f"{target}page={page}  transport={transport}  items={items}  "
            f"cursor={cursor_status}{http_part}{latency_part}{attempt_part}{recovery_part}{arrow}"
        )

    def pagination(self, account: str, endpoint: str, page: int, cursor: Optional[str]) -> None:
        if self.verbosity == Verbosity.VERBOSE:
            cursor_text = "found" if cursor else "end"
            self.info(f"@{account} | {endpoint} | page {page} | next_cursor={cursor_text}")

    def show_startup_config(
        self,
        config: Dict[str, Any],
        account_map: Dict[str, Dict],
        policies: Dict[int, Dict],
        config_path: str = "config/config.json",
    ) -> None:
        api_cfg = config.get("api_config", {})
        if self.rich_enabled and Table is not None:
            table = Table(title=f"{self._prefix()} API / Tier Configuration", show_lines=False)
            table.add_column("Key", style="cyan")
            table.add_column("Value", style="white")
            table.add_row("Config File", config_path)
            table.add_row("Accounts (tiered)", str(len(account_map)))
            table.add_row("Priority Policies", str(len(policies)))
            table.add_row(
                "UserByScreenName QueryID",
                str(api_cfg.get("user_by_screen_name_query_id", ""))[:20] + "...",
            )
            table.add_row(
                "UserTweets QueryID",
                str(api_cfg.get("user_tweets_query_id", ""))[:20] + "...",
            )
            table.add_row(
                "UserTweetsAndReplies QueryID",
                str(api_cfg.get("user_tweets_and_replies_query_id", ""))[:20] + "...",
            )
            table.add_row("Timeout (sec)", str(api_cfg.get("default_timeout_seconds", 20)))
            self._console.print(table)
        else:
            self.info(f"Config File: {config_path}")
            self.info(f"Accounts (tiered): {len(account_map)}")
            self.info(f"Priority Policies: {len(policies)}")
            self.info(f"Timeout (sec): {api_cfg.get('default_timeout_seconds', 20)}")

    def account_summary_table(self, username: str, account_report: Dict[str, Any]) -> None:
        status = account_report.get("status", "unknown")
        sets = account_report.get("sets", {})
        new_tweets = account_report.get("new_tweets", {})
        endpoints = account_report.get("endpoints", {})

        if self.rich_enabled and Table is not None:
            table = Table(title=f"@{username} [{status.upper()}]", show_lines=False)
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="white")
            table.add_row("Status", status)
            if account_report.get("priority") is not None:
                table.add_row("Priority", str(account_report.get("priority", "")))
            if sets:
                table.add_row("Tweets (union)", str(sets.get("4_union", 0)))
            if new_tweets:
                table.add_row("New this cycle", str(new_tweets.get("new", 0)))
                table.add_row("Duplicates", str(new_tweets.get("duplicates", 0)))
                table.add_row("Viral Reports", str(new_tweets.get("viral_reports", 0)))
            for ep, ep_data in endpoints.items():
                pages = ep_data.get("pages_fetched", 0)
                http_status = ep_data.get("last_http_status", "N/A")
                outcome = ep_data.get("outcome", "")
                cursor_reason = ep_data.get("cursor_termination_reason", "")
                cov = ep_data.get("window_coverage") or {}
                cov_text = ""
                if cov:
                    oldest = cov.get("oldest_date", "?")
                    newest = cov.get("newest_date", "?")
                    cov_text = f" dates={oldest}..{newest}"
                table.add_row(
                    f"Endpoint: {ep}",
                    f"pages={pages} http={http_status} outcome={outcome}{cov_text}",
                )
                if cursor_reason:
                    table.add_row(f"  {ep} cursor", cursor_reason)
            self._console.print(table)
        else:
            print(f"{self._prefix()} @{username} [{status.upper()}]")
            for ep, ep_data in endpoints.items():
                outcome = ep_data.get("outcome", "")
                pages = ep_data.get("pages_fetched", 0)
                print(f"{self._prefix()}   {ep}: pages={pages} outcome={outcome}")

    def search_summary(self, report: Dict[str, Any]) -> None:
        if self.rich_enabled and Table is not None:
            table = Table(
                title=f"Search: {report.get('search', 'Unknown')}",
                show_lines=False,
            )
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="white")
            table.add_row("Slug", report.get("slug", ""))
            table.add_row("Product", report.get("product", ""))
            table.add_row("Status", report.get("status", ""))
            table.add_row("Tweets", str(report.get("counts", {}).get("tweets", 0)))
            meta = report.get("metadata", {})
            table.add_row("Pages", f"{meta.get('pages_saved', 0)}/{meta.get('pages_requested', 0)}")
            table.add_row("Exhausted", str(meta.get("exhausted_reason", "")))
            last_status = meta.get("last_http_status")
            table.add_row("Last HTTP", str(last_status) if last_status else "N/A")
            self._console.print(table)
        else:
            self.info(f"Search {report.get('search')}: {report.get('status')}")

# === File logging =========================================================



# Single root logger for the whole package. Child loggers (console.<sub>,
# twitter.client, pipelines.historical.service, ...) propagate to it.
ROOT_LOGGER_NAME = "fetcher"
CONSOLE_LOGGER_PREFIX = f"{ROOT_LOGGER_NAME}.console"

# Map semantic verbosity to the stderr tail level. The file handler is always
# DEBUG so the on-disk record is complete regardless of terminal noise.
_VERBOSITY_TO_LEVEL = {
    Verbosity.QUIET: logging.WARNING,
    Verbosity.NORMAL: logging.INFO,
    Verbosity.VERBOSE: logging.DEBUG,
}

_FILE_FMT = logging.Formatter(
    "%(asctime)s run=%(run_id)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
_STDERR_FMT = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Module-global handles so configure_logging is idempotent and attach_run_id
# can mutate the live filter without the caller threading the object around.
_RUN_FILTER: Optional["RunIdFilter"] = None
_CONFIGURED = False


class RunIdFilter(logging.Filter):
    """Stamp every log record with ``record.run_id`` (default ``"-"``)."""

    def __init__(self, run_id: str = "-") -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


class _NoConsoleOnStderr(logging.Filter):
    """Drop console-subsystem records from the stderr tail.

    :class:`PipelineConsole` already prints those lines to the Rich terminal,
    so echoing them to stderr would double-print. The root file handler is
    unaffected (records still propagate there), keeping the log file complete.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(CONSOLE_LOGGER_PREFIX)


def configure_logging(
    *,
    subsystem: str,
    logs_dir: Optional[Path],
    verbosity: Verbosity = Verbosity.NORMAL,
    run_id: Optional[str] = None,
) -> logging.Logger:
    """Configure the package root logger once (idempotent).

    First caller wins for ``subsystem``/``logs_dir``/``verbosity`` (the CLI
    ``main`` runs before the engine is built and sets the right values).
    Subsequent calls still update ``run_id``. Safe to call with ``logs_dir``
    ``None`` (stderr-only) — used by tests/CLIs that don't persist logs.
    """
    global _RUN_FILTER, _CONFIGURED
    root = logging.getLogger(ROOT_LOGGER_NAME)

    if not _CONFIGURED:
        # Clean slate: a previous basicConfig/test run may have added handlers.
        for handler in list(root.handlers):
            root.removeHandler(handler)

        _RUN_FILTER = RunIdFilter(run_id or "-")
        stderr_handler = logging.StreamHandler(stream=sys.stderr)
        stderr_handler.setLevel(_VERBOSITY_TO_LEVEL.get(verbosity, logging.INFO))
        stderr_handler.setFormatter(_STDERR_FMT)
        stderr_handler.addFilter(_NoConsoleOnStderr())
        stderr_handler.addFilter(_RUN_FILTER)
        root.addHandler(stderr_handler)

        if logs_dir is not None:
            logs_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                logs_dir / f"{subsystem}.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(_FILE_FMT)
            file_handler.addFilter(_RUN_FILTER)
            root.addHandler(file_handler)

        root.setLevel(logging.DEBUG)
        root.propagate = False  # we own stderr; don't bubble to logging.root
        _CONFIGURED = True
    elif run_id and _RUN_FILTER is not None:
        _RUN_FILTER.run_id = run_id

    return root


def attach_run_id(run_id: Optional[str]) -> None:
    """Stamp ``run_id`` onto every subsequent log record.

    Call once the run id is known (it is created *after* the engine is built
    in the historical pipeline). No-op before :func:`configure_logging`.
    """
    if _RUN_FILTER is not None and run_id:
        _RUN_FILTER.run_id = run_id


def reset_logging() -> None:
    """Tear down configured handlers (test-only isolation)."""
    global _RUN_FILTER, _CONFIGURED
    root = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass
    _RUN_FILTER = None
    _CONFIGURED = False


# === Structured events ====================================================


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
