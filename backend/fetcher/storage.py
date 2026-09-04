"""Scratch-disk layer: raw GraphQL pages, processed tweet sets, reports, state.

The engine runs in an ephemeral scratch root and this module is how it gets
data onto disk there; ``fetching.runner`` reads the results back out and moves
them into Postgres, which is the durable store.

Only one processed set is produced (``4_union``, the complete per-account set).
Batch and report names use Tehran-local dates so day boundaries match the
rolling-window logic in ``fetcher.processing``.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from fetcher.config import PROJECT_ROOT, read_json, write_json
from fetcher.clock import utc_now, utc_now_iso

UNION_SET = "4_union"
ENDPOINTS = ("UserTweets", "UserTweetsAndReplies")


class StorageManager:
    """Manage raw GraphQL pages and processed tweet set outputs."""

    def __init__(
        self,
        project_root: Optional[Path] = None,
        base_dir: Optional[Path] = None,
        timezone: str = "Asia/Tehran",
        subsystem: str = "historical",
        create_folders: bool = True,
        manage_sync_state: bool = True,
        data_root_override: Optional[Path] = None,
    ):
        self.project_root = project_root or base_dir or PROJECT_ROOT
        self.timezone = timezone
        try:
            self.tz = ZoneInfo(timezone)
        except Exception:  # pragma: no cover - missing tzdata
            self.tz = None

        raw_sub = str(subsystem or "historical").strip().lower()
        # Historical and live deliberately share one data tree.
        self.subsystem = "historical_live" if raw_sub in ("historical", "live") else raw_sub

        self.global_data_root = data_root_override or (self.project_root / "data")
        self.data_root = self.global_data_root / self.subsystem
        self.raw_root = self.data_root / "raw"
        self.processed_root = self.data_root / "processed"
        self.state_dir = self.data_root / "state"
        self.reports_dir = self.data_root / "reports"
        self.logs_dir = self.data_root / "logs"
        self.merged_dir = self.processed_root / UNION_SET

        self.sync_state_file: Optional[Path] = (
            self.state_dir / "sync_state.json" if manage_sync_state else None
        )
        if manage_sync_state or create_folders:
            dirs = [self.raw_root, self.processed_root, self.state_dir, self.reports_dir, self.logs_dir]
            if self.subsystem == "historical_live":
                dirs.append(self.merged_dir)
            for path in dirs:
                path.mkdir(parents=True, exist_ok=True)

    # --- Time and naming ---------------------------------------------------

    def _now(self) -> datetime:
        return datetime.now(self.tz) if self.tz else utc_now()

    def _batch_name(self) -> str:
        return self._now().strftime("%Y-%m-%d_%H-%M")

    def create_run_id(self) -> str:
        return f"run_{self._now().strftime('%Y-%m-%d_%H-%M-%S')}"

    @staticmethod
    def _normalize_username(username: str) -> str:
        return (username or "unknown").strip().lstrip("@").lower() or "unknown"

    @staticmethod
    def _page_sort_key(name: str) -> int:
        match = re.search(r"page_(\d+)\.json$", name)
        return int(match.group(1)) if match else 0

    # --- Reports -----------------------------------------------------------

    def create_run_report_paths(self, run_id: str) -> Dict[str, Path]:
        safe_run_id = re.sub(r"[^a-zA-Z0-9_\-]+", "_", str(run_id or self.create_run_id()))
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        return {
            "json": self.reports_dir / f"{safe_run_id}.json",
            "txt": self.reports_dir / f"{safe_run_id}.txt",
        }

    def save_run_report_json(self, report: Dict[str, Any], run_id: str) -> Path:
        path = self.create_run_report_paths(run_id)["json"]
        return write_json(path, report if isinstance(report, dict) else {})

    def save_run_report_txt(self, report: Dict[str, Any], run_id: str) -> Path:
        """Human-readable run summary. The runner parses the JSON twin, not this."""
        path = self.create_run_report_paths(run_id)["txt"]
        summary = report.get("summary", {}) if isinstance(report, dict) else {}
        accounts = report.get("accounts", {}) if isinstance(report, dict) else {}
        lines = [
            f"Run ID: {report.get('run_id', run_id)}",
            f"Started: {report.get('started_at', 'UNKNOWN')}",
            f"Finished: {report.get('finished_at', 'UNKNOWN')}",
            "",
            "Summary",
            *(
                f"  {label}: {summary.get(key, 0)}"
                for label, key in (
                    ("Successful endpoints", "successful_endpoints"),
                    ("Partial endpoints", "partial_endpoints"),
                    ("Failed endpoints", "failed_endpoints"),
                    ("Skipped endpoints", "skipped_endpoints"),
                )
            ),
            "",
        ]
        for username in sorted(accounts, key=str.lower):
            account = accounts.get(username, {})
            lines.append(f"@{username}")
            if account.get("user_id"):
                lines.append(f"  user_id: {account['user_id']}")
            if account.get("skip_reason"):
                lines.append(f"  skipped: {account['skip_reason']}")
            for endpoint, report_row in (account.get("endpoints", {}) or {}).items():
                lines.append(
                    f"  {endpoint}: status={report_row.get('status')} "
                    f"outcome={report_row.get('outcome')} "
                    f"pages={report_row.get('pages_fetched', 0)} "
                    f"verified={report_row.get('processed_verified', False)}"
                )
                if report_row.get("reason"):
                    lines.append(f"    reason: {report_row['reason']}")
            final_sets = account.get("final_sets")
            if final_sets:
                lines.append(
                    f"  final_sets: verified={final_sets.get('verified')} "
                    f"counts={final_sets.get('counts', {})}"
                )
            lines.append("")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return path

    # --- Sync state (cursors, watermarks, user ids) ------------------------

    def load_sync_state(self) -> Dict[str, Any]:
        return read_json(self.sync_state_file, {}) if self.sync_state_file else {}

    def save_sync_state(self, state: Dict[str, Any]) -> Path:
        return write_json(self.sync_state_file, state if isinstance(state, dict) else {})

    def _account_state(self, state: Dict[str, Any], username: str) -> Dict[str, Any]:
        if not isinstance(state.get(username), dict):
            state[username] = {}
        return state[username]

    def get_endpoint_state(self, username: str, endpoint: str) -> Dict[str, Any]:
        """Endpoint sync status; defaults to ``{"last_cursor": None, "status": "pending"}``."""
        user_state = self.load_sync_state().get(self._normalize_username(username), {})
        endpoint_state = user_state.get(endpoint, {}) if isinstance(user_state, dict) else {}
        if not isinstance(endpoint_state, dict):
            endpoint_state = {}
        return {"last_cursor": None, "status": "pending", **endpoint_state}

    def update_endpoint_state(
        self,
        username: str,
        endpoint: str,
        *,
        last_cursor: Optional[str] = None,
        status: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Path:
        state = self.load_sync_state()
        account = self._account_state(state, self._normalize_username(username))
        if not isinstance(account.get(endpoint), dict):
            account[endpoint] = {"last_cursor": None, "status": "pending"}
        if last_cursor is not None:
            account[endpoint]["last_cursor"] = last_cursor
        if status is not None:
            account[endpoint]["status"] = status
        if isinstance(meta, dict):
            account[endpoint].update(meta)
        return self.save_sync_state(state)

    def get_fetch_watermark(self, username: str, endpoint: str) -> Optional[str]:
        """ISO-8601 time of the last SUCCESSFUL fetch, or None before the first.

        Drives the unified rolling window: the next run backfills past the
        floored watermark so no partial day/hour is ever skipped.
        """
        value = self.get_endpoint_state(username, endpoint).get("fetch_watermark")
        return str(value) if value else None

    def set_fetch_watermark(self, username: str, endpoint: str, iso_timestamp: str) -> Path:
        """Only call on a SUCCESSFUL fetch, so a failed run never advances past
        unfetched data."""
        return self.update_endpoint_state(
            username, endpoint, meta={"fetch_watermark": str(iso_timestamp)}
        )

    def get_user_id(self, username: str) -> Optional[str]:
        user_state = self.load_sync_state().get(self._normalize_username(username), {})
        value = user_state.get("user_id") if isinstance(user_state, dict) else None
        return str(value) if value else None

    def set_user_id(self, username: str, user_id: str) -> Path:
        state = self.load_sync_state()
        account = self._account_state(state, self._normalize_username(username))
        account["user_id"] = str(user_id)
        for key in ("skip_current_run", "skip_reason", "skip_at"):
            account.pop(key, None)
        return self.save_sync_state(state)

    def update_account_state(self, username: str, mutator: Callable[[Dict[str, Any]], None]) -> Path:
        state = self.load_sync_state()
        mutator(self._account_state(state, self._normalize_username(username)))
        return self.save_sync_state(state)

    def mark_account_skipped_for_run(self, username: str, reason: str) -> Path:
        now = utc_now_iso()

        def mutate(account: Dict[str, Any]) -> None:
            account.update({"skip_current_run": True, "skip_reason": reason, "skip_at": now})
            for endpoint in ENDPOINTS:
                if not isinstance(account.get(endpoint), dict):
                    account[endpoint] = {}
                account[endpoint].update(
                    {"status": "skipped", "skip_reason": reason, "skipped_at": now}
                )

        return self.update_account_state(username, mutate)

    def ensure_account_state(self, username: str) -> Path:
        def mutate(account: Dict[str, Any]) -> None:
            for endpoint in ENDPOINTS:
                if not isinstance(account.get(endpoint), dict):
                    account[endpoint] = {"last_cursor": None, "status": "pending"}

        return self.update_account_state(username, mutate)

    # --- Raw pages ---------------------------------------------------------

    def create_raw_batch_dir(
        self, endpoint_name: str, username: str, batch_name: Optional[str] = None
    ) -> Path:
        target = (
            self.raw_root
            / endpoint_name
            / self._normalize_username(username)
            / (batch_name or self._batch_name())
        )
        target.mkdir(parents=True, exist_ok=True)
        return target

    def save_raw_page(self, batch_dir: Path, page_number: int, payload: Dict[str, Any]) -> Path:
        return write_json(
            Path(batch_dir) / f"page_{int(page_number)}.json",
            payload if isinstance(payload, dict) else {},
        )

    def save_search_result_page(
        self, search_slug: str, product: str, batch: str, page_index: int, payload: Dict[str, Any]
    ) -> Path:
        """Search pages live at ``data/search/raw/{slug}/{product}/{batch}/page_N.json``
        and must never touch the historical/live set folders."""
        target = self.raw_root / search_slug / product.lower() / batch
        return write_json(
            target / f"page_{page_index}.json", payload if isinstance(payload, dict) else {}
        )

    def load_raw_pages_from_batch(self, batch_path: Any) -> List[Dict[str, Any]]:
        path = Path(str(batch_path)) if batch_path else Path()
        if not path.is_dir():
            return []
        pages = []
        for page_file in sorted(path.glob("page_*.json"), key=lambda p: self._page_sort_key(p.name)):
            payload = read_json(page_file)
            if isinstance(payload, dict):
                pages.append(payload)
        return pages

    def find_raw_batches(self, endpoint_name: str, username: str) -> List[Path]:
        root = self.raw_root / endpoint_name / self._normalize_username(username)
        return sorted(path for path in root.iterdir() if path.is_dir()) if root.exists() else []

    def load_all_raw_pages(self, endpoint_name: str, username: str) -> List[Dict[str, Any]]:
        pages: List[Dict[str, Any]] = []
        for batch_dir in self.find_raw_batches(endpoint_name, username):
            pages.extend(self.load_raw_pages_from_batch(batch_dir))
        return pages

    def prune_raw_batches(self, endpoint_name: str, username: str, *, keep: int = 3) -> int:
        """Delete older raw batch dirs, keeping the newest ``keep`` (0 = unlimited)."""
        if keep <= 0:
            return 0
        batches = self.find_raw_batches(endpoint_name, username)
        removed = 0
        for batch_dir in batches[: max(0, len(batches) - keep)]:
            try:
                shutil.rmtree(batch_dir)
                removed += 1
            except OSError:
                continue
        return removed

    # --- Processed sets ----------------------------------------------------

    def _set_dir(self, set_name: str, username: str) -> Path:
        if str(set_name).strip() != UNION_SET:
            raise ValueError(f"Unsupported set_name: {set_name}")
        return self.processed_root / UNION_SET / self._normalize_username(username)

    def save_processed_set(self, data_list: List[Dict[str, Any]], set_name: str, username: str) -> Path:
        target = self._set_dir(set_name, username)
        return write_json(
            target / f"{UNION_SET}.json", data_list if isinstance(data_list, list) else []
        )

    def load_processed_set(self, set_name: str, username: str) -> List[Dict[str, Any]]:
        payload = read_json(self._set_dir(set_name, username) / f"{UNION_SET}.json", [])
        return payload if isinstance(payload, list) else []

    def merge_processed_items(
        self, existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Dedupe by ``author_id:tweet_id``, falling back to the tweet id alone."""
        merged: Dict[str, Dict[str, Any]] = {}
        anonymous: List[Dict[str, Any]] = []
        for item in list(existing or []) + list(incoming or []):
            if not isinstance(item, dict):
                continue
            tweet_id = str(item.get("rest_id") or item.get("id") or item.get("tweet_id") or "").strip()
            author_id = str(item.get("author_id") or "").strip()
            if tweet_id and author_id:
                tweet_id = f"{author_id}:{tweet_id}"
            if tweet_id:
                merged[tweet_id] = item
            else:
                anonymous.append(item)
        return list(merged.values()) + anonymous

    def save_processed_set_merged(
        self, data_list: List[Dict[str, Any]], set_name: str, username: str
    ) -> Path:
        """Merge into the account's existing set so a run accumulates rather
        than overwriting what earlier runs already captured."""
        merged = self.merge_processed_items(
            self.load_processed_set(set_name, username),
            data_list if isinstance(data_list, list) else [],
        )
        return self.save_processed_set(merged, set_name, username)
