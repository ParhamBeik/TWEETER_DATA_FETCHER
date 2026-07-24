#!/usr/bin/env python3
"""Scan raw JSON datastore and cross-reference sync state watermarks.

Run:
    tdf-coverage --format table
    python -m tweeter_data_fetcher.observability.coverage_inventory --account elonmusk --format json

Flags:
    --account <user>       filter to account(s) (repeatable / comma-separated)
    --endpoint <name>      filter to one endpoint
    --format {table,json}  output format (default table)
    --export <path>        write the report to a file
    --config <path>        config.json to use (else canonical config/)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from tweeter_data_fetcher.processing.core import TweetSetProcessor, tweet_jalali_date
from tweeter_data_fetcher.storage.facade import StorageManager
from tweeter_data_fetcher.configuration import load_tier_config, ordered_accounts, resolve_config_path
from tweeter_data_fetcher.observability.logging_setup import configure_logging
from tweeter_data_fetcher.observability.pipeline_console import PipelineConsole
from tweeter_data_fetcher.paths import PROJECT_ROOT


ENDPOINTS = ("UserTweets", "UserTweetsAndReplies")


@dataclass
class EndpointCoverage:
    account: str
    endpoint: str
    batches: int = 0
    pages: int = 0
    oldest_date: Optional[str] = None
    newest_date: Optional[str] = None
    covered_dates: List[str] = field(default_factory=list)
    batch_paths: List[str] = field(default_factory=list)
    watermark: Optional[str] = None
    endpoint_status: str = "unknown"
    sync_status: str = "pending"
    outcome: Optional[str] = None

    @property
    def date_range(self) -> str:
        if self.oldest_date and self.newest_date:
            return f"{self.oldest_date} .. {self.newest_date}"
        return ""

    @property
    def status_label(self) -> str:
        if self.sync_status == "completed":
            return "complete"
        if self.sync_status in {"partial", "failed"}:
            missing = ""
            return f"{self.sync_status}{missing}"
        if self.pages > 0:
            return "has_data"
        return "no_data"

    def to_row(self) -> Dict[str, Any]:
        return {
            "account": self.account,
            "endpoint": self.endpoint,
            "batches": self.batches,
            "pages": self.pages,
            "date_range": self.date_range,
            "watermark": self.watermark or "",
            "status": self.status_label,
            "covered_dates": self.covered_dates,
            "batch_paths": self.batch_paths,
            "sync_status": self.sync_status,
            "outcome": self.outcome,
        }


class CoverageInventory:
    """Inventory scanner for historical_live raw pages."""

    def __init__(self, storage: StorageManager, processor: Optional[TweetSetProcessor] = None) -> None:
        self.storage = storage
        self.processor = processor or TweetSetProcessor()

    def scan_endpoint(self, username: str, endpoint: str) -> EndpointCoverage:
        uname = self.storage._normalize_username(username)
        cov = EndpointCoverage(account=uname, endpoint=endpoint)
        batches = self.storage.find_raw_batches(endpoint, uname)
        cov.batches = len(batches)
        cov.batch_paths = [str(path) for path in batches]

        all_dates: Set[str] = set()
        total_pages = 0
        for batch_dir in batches:
            pages = self.storage.load_raw_pages_from_batch(batch_dir)
            total_pages += len(pages)
            tweets = self.processor.extract_tweets_from_raw(pages, username=uname, source_endpoint=endpoint)
            for tweet in tweets.values():
                jdate = tweet_jalali_date(tweet)
                if jdate:
                    all_dates.add(jdate)

        cov.pages = total_pages
        if all_dates:
            sorted_dates = sorted(all_dates)
            cov.covered_dates = sorted_dates
            cov.oldest_date = sorted_dates[0]
            cov.newest_date = sorted_dates[-1]

        ep_state = self.storage.get_endpoint_state(uname, endpoint)
        cov.watermark = ep_state.get("fetch_watermark")
        cov.sync_status = str(ep_state.get("status", "pending"))
        cov.outcome = ep_state.get("outcome")
        return cov

    def scan_account(self, username: str, endpoints: Optional[List[str]] = None) -> List[EndpointCoverage]:
        eps = endpoints or list(ENDPOINTS)
        return [self.scan_endpoint(username, ep) for ep in eps]

    def scan_all(self, accounts: List[str], endpoints: Optional[List[str]] = None) -> List[EndpointCoverage]:
        results: List[EndpointCoverage] = []
        for account in accounts:
            results.extend(self.scan_account(account, endpoints))
        return results

    def build_index(self, accounts: List[str]) -> Dict[str, Any]:
        rows = self.scan_all(accounts)
        return {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "accounts": {
                row.account: {
                    **({} if row.endpoint not in {r.endpoint for r in rows if r.account == row.account} else {}),
                }
                for row in rows
            },
            "endpoints": [row.to_row() for row in rows],
        }

    def save_index(self, accounts: List[str], output_path: Optional[Path] = None) -> Path:
        index = self.build_index(accounts)
        target = output_path or (self.storage.state_dir / "coverage_index.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        import json

        with target.open("w", encoding="utf-8") as handle:
            json.dump(index, handle, ensure_ascii=False, indent=2)
        return target

    @staticmethod
    def coverage_summary_text(coverage: Optional[Dict[str, Any]]) -> str:
        if not coverage:
            return ""
        oldest = coverage.get("oldest_date")
        newest = coverage.get("newest_date")
        target_dates = coverage.get("target_dates") or []
        covered_dates = coverage.get("covered_dates") or []
        missing_dates = coverage.get("missing_dates") or []
        if oldest and newest:
            base = f"{oldest}..{newest}"
            if target_dates and covered_dates:
                return f"{base} ({len(covered_dates)}/{len(target_dates)} days)"
            if missing_dates:
                return f"{base} (missing: {', '.join(missing_dates[:3])})"
            return base
        return str(coverage.get("reason", ""))


def _load_accounts(config_path: Path, only: Optional[List[str]]) -> List[str]:
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    account_map, _ = load_tier_config(config)
    accounts = ordered_accounts(account_map)
    if not only:
        return accounts
    selected = {account.strip().lstrip("@").lower() for account in only}
    return [account for account in accounts if account.lower() in selected]


def run_coverage_status(
    *,
    config_path: Optional[str] = None,
    only_accounts: Optional[List[str]] = None,
    only_endpoint: Optional[str] = None,
    output_format: str = "table",
    export_path: Optional[str] = None,
) -> int:
    storage = StorageManager(base_dir=PROJECT_ROOT, subsystem="historical_live")
    configure_logging(subsystem="historical_live", logs_dir=storage.logs_dir)
    console = PipelineConsole("historical")
    accounts = _load_accounts(resolve_config_path(config_path, project_root=PROJECT_ROOT), only_accounts)
    endpoints = [only_endpoint] if only_endpoint else list(ENDPOINTS)
    rows = [
        coverage.to_row()
        for account in accounts
        for coverage in CoverageInventory(storage).scan_account(account, endpoints)
    ]

    if output_format == "json":
        text = json.dumps(
            {"generated_at": storage.create_run_id().replace("run_", ""), "rows": rows},
            ensure_ascii=False,
            indent=2,
        )
        if export_path:
            Path(export_path).write_text(text + "\n", encoding="utf-8")
            console.success(f"Wrote JSON report: {export_path}")
        else:
            print(text)
        return 0

    console.coverage_table(rows, title="Historical/Live Raw Data Coverage")
    target = (
        Path(export_path)
        if export_path
        else storage.reports_dir / f"coverage_{storage._jalali_batch_name()}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    console.success(f"Wrote report: {target}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Report raw JSON coverage per account and endpoint.")
    parser.add_argument("--account", action="append", default=[], help="Filter to account(s).")
    parser.add_argument("--endpoint", choices=list(ENDPOINTS), help="Filter to one endpoint.")
    parser.add_argument("--format", choices=["table", "json"], default="table")
    parser.add_argument("--export", dest="export_path", help="Optional export file path.")
    parser.add_argument("--config")
    args = parser.parse_args()
    only = [part.strip() for value in args.account for part in value.split(",") if part.strip()] or None
    sys.exit(
        run_coverage_status(
            config_path=args.config,
            only_accounts=only,
            only_endpoint=args.endpoint,
            output_format=args.format,
            export_path=args.export_path,
        )
    )


if __name__ == "__main__":
    main()
