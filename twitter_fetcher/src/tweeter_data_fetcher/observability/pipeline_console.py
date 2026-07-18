#!/usr/bin/env python3
"""Rich-first terminal console with subsystem tags and structured phase/page output."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, List, Optional

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
        self._logger = logging.getLogger(f"tweeter_data_fetcher.console.{self.subsystem}")

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

    def fetch_context(
        self,
        *,
        account: str,
        endpoint: str,
        user_id: Optional[str] = None,
        watermark: Optional[str] = None,
        cutoff: Optional[str] = None,
        account_index: Optional[int] = None,
        account_total: Optional[int] = None,
    ) -> None:
        if self.verbosity == Verbosity.QUIET:
            return
        idx = ""
        if account_index is not None and account_total is not None:
            idx = f"[{account_index}/{account_total}] "
        lines = [f"{idx}@{account}  endpoint={endpoint}"]
        if user_id:
            lines.append(f"  user_id={user_id}")
        if watermark:
            lines.append(f"  watermark={watermark}")
        if cutoff:
            lines.append(f"  cutoff={cutoff}")
        for line in lines:
            self.info(line)

    def page_row(
        self,
        *,
        page: int,
        items: int,
        cursor_status: str,
        http_status: Optional[int] = None,
        next_page: Optional[int] = None,
    ) -> None:
        if self.verbosity == Verbosity.QUIET:
            return
        http_part = f"  http={http_status}" if http_status is not None else ""
        arrow = f" → page {next_page}" if next_page else ""
        self.info(f"  page {page}  items={items}  cursor={cursor_status}{http_part}{arrow}")

    def fetch_outcome(
        self,
        *,
        account: str,
        endpoint: str,
        status: str,
        outcome: str,
        reason: str,
        pages_fetched: int = 0,
        coverage_summary: Optional[str] = None,
    ) -> None:
        cov = f"  coverage: {coverage_summary}" if coverage_summary else ""
        if status == "completed":
            self.success(f"@{account} {endpoint}: {outcome} ({pages_fetched} pages){cov}")
        elif status == "partial":
            self.warning(f"@{account} {endpoint}: {outcome} — {reason} ({pages_fetched} pages){cov}")
        else:
            self.error(f"@{account} {endpoint}: {outcome} — {reason}")

    def pagination(self, account: str, endpoint: str, page: int, cursor: Optional[str]) -> None:
        if self.verbosity == Verbosity.VERBOSE:
            cursor_text = cursor if cursor else "END"
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
                table.add_row("Tweets (intersection)", str(sets.get("3_intersection", 0)))
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

    def coverage_table(self, rows: List[Dict[str, Any]], title: str = "Coverage Inventory") -> None:
        if not rows:
            self.warning("No coverage data to display")
            return
        if self.rich_enabled and Table is not None:
            table = Table(title=f"{self._prefix()} {title}", show_lines=False)
            for col in ("Account", "Endpoint", "Batches", "Pages", "Date Range", "Watermark", "Status"):
                table.add_column(col, style="cyan" if col == "Account" else "white")
            for row in rows:
                table.add_row(
                    row.get("account", ""),
                    row.get("endpoint", ""),
                    str(row.get("batches", 0)),
                    str(row.get("pages", 0)),
                    row.get("date_range", ""),
                    row.get("watermark", ""),
                    row.get("status", ""),
                )
            self._console.print(table)
        else:
            self.banner(title)
            for row in rows:
                self.info(
                    f"@{row.get('account')} {row.get('endpoint')}: "
                    f"{row.get('pages')} pages, {row.get('date_range')}, {row.get('status')}"
                )
