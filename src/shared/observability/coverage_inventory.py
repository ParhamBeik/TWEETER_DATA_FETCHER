#!/usr/bin/env python3
"""Scan raw JSON datastore and cross-reference sync state watermarks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.shared.core.tweet_processing_utils import TweetSetProcessor, tweet_jalali_date
from src.shared.data_pipeline.storage_manager import StorageManager


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
