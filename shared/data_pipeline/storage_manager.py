#!/usr/bin/env python3
"""
Storage manager for Phase 3 raw/processed persistence.
"""

from __future__ import annotations

import json
import re
import shutil
import textwrap
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import jdatetime
except ImportError:
    jdatetime = None

try:
    import pytz
except ImportError:
    pytz = None

from exporters.text_export_helper import choose_export_text


def extract_metrics(tweet_obj: Dict[str, Any]) -> Dict[str, Any]:
    """Compatibility helper for legacy callers expecting metric extraction."""
    legacy = tweet_obj.get("legacy", {}) if isinstance(tweet_obj, dict) else {}
    views_obj = tweet_obj.get("views", {}) if isinstance(tweet_obj, dict) else {}
    views_raw = views_obj.get("count", 0) if isinstance(views_obj, dict) else 0
    try:
        views = int(str(views_raw).replace(",", ""))
    except (TypeError, ValueError):
        views = 0
    return {
        "likes": legacy.get("favorite_count", 0),
        "retweets": legacy.get("retweet_count", 0),
        "replies": legacy.get("reply_count", 0),
        "quotes": legacy.get("quote_count", 0),
        "bookmarks": legacy.get("bookmark_count", 0),
        "views": views,
    }


def _gregorian_to_jalali(year: int, month: int, day: int) -> tuple[int, int, int]:
    """Convert Gregorian date to Jalali date; fallback for missing jdatetime."""
    g_days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    j_days_in_month = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]
    gy = year - 1600
    gm = month - 1
    gd = day - 1
    g_day_no = 365 * gy + (gy + 3) // 4 - (gy + 99) // 100 + (gy + 399) // 400
    for idx in range(gm):
        g_day_no += g_days_in_month[idx]
    if gm > 1 and ((gy + 1600) % 4 == 0 and ((gy + 1600) % 100 != 0 or (gy + 1600) % 400 == 0)):
        g_day_no += 1
    g_day_no += gd
    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    jm = 0
    while jm < 11 and j_day_no >= j_days_in_month[jm]:
        j_day_no -= j_days_in_month[jm]
        jm += 1
    return jy, jm + 1, j_day_no + 1


def _format_jalali(dt: datetime, fmt: str) -> str:
    if jdatetime:
        return jdatetime.datetime.fromgregorian(datetime=dt).strftime(fmt)
    jy, jm, jd = _gregorian_to_jalali(dt.year, dt.month, dt.day)
    return (
        fmt.replace("%Y", f"{jy:04d}")
        .replace("%m", f"{jm:02d}")
        .replace("%d", f"{jd:02d}")
        .replace("%H", f"{dt.hour:02d}")
        .replace("%M", f"{dt.minute:02d}")
        .replace("%S", f"{dt.second:02d}")
    )


class StorageManager:
    """Manage raw GraphQL pages and processed tweet set outputs."""

    SET_FOLDER_MAP: Dict[str, str] = {
        "A": "1_user_tweets",
        "B": "2_user_tweets_and_replies",
        "INTERSECTION": "3_intersection",
        "UNION": "4_union",
        "REPLIES_ONLY": "5_replies_only",
        "1_user_tweets": "1_user_tweets",
        "2_user_tweets_and_replies": "2_user_tweets_and_replies",
        "3_intersection": "3_intersection",
        "4_union": "4_union",
        "5_replies_only": "5_replies_only",
        "1_user_tweets (A)": "1_user_tweets",
        "2_user_tweets_and_replies (B)": "2_user_tweets_and_replies",
        "3_intersection (A ∩ B)": "3_intersection",
        "4_union (A ∪ B)": "4_union",
        "5_replies_only (B - A)": "5_replies_only",
    }

    def __init__(
        self,
        project_root: Optional[Path] = None,
        base_dir: Optional[Path] = None,
        timezone: str = "Asia/Tehran",
        subsystem: str = "historical",
    ):
        # `base_dir`/`timezone` kept for backward compatibility with Phase-2 engine.
        self.project_root = project_root or base_dir or Path(__file__).resolve().parent.parent
        self.timezone = timezone
        self.tz = pytz.timezone(timezone) if pytz else None
        self.subsystem = str(subsystem or "historical").strip().lower()
        self.global_data_root = self.project_root / "data"
        self.data_root = self.global_data_root / self.subsystem
        self.raw_root = self.data_root / "raw"
        self.processed_root = self.data_root / "processed"
        self.state_dir = self.data_root / "state"
        self.reports_dir = self.data_root / "reports"
        self.legacy_data_root = self.global_data_root
        self.legacy_raw_root = self.legacy_data_root / "raw_json"
        self.legacy_processed_root = self.legacy_data_root / "processed"
        self.legacy_reports_dir = self.legacy_data_root / "reports"
        self.global_state_dir = self.global_data_root / "state"
        self.legacy_state_dir = self.legacy_data_root / "STATE"
        self.sync_state_file = self.state_dir / "sync_state.json"
        self.legacy_sync_state_file = self.legacy_state_dir / "sync_state.json"

        # Compatibility aliases used by existing fetching code.
        self.raw_user_tweets_dir = self.raw_root / "UserTweets"
        self.raw_user_replies_dir = self.raw_root / "UserTweetsAndReplies"
        self.user_tweets_dir = self.processed_root / "1_user_tweets"
        self.user_replies_dir = self.processed_root / "2_user_tweets_and_replies"
        self.intersection_dir = self.processed_root / "3_intersection"
        self.merged_dir = self.processed_root / "4_union"
        self.endpoint_diffs_dir = self.processed_root / "5_replies_only"
        self.logs_dir = self.data_root / "logs"

        self._ensure_base_dirs()

    def _ensure_base_dirs(self) -> None:
        for path in [
            self.raw_user_tweets_dir,
            self.raw_user_replies_dir,
            self.user_tweets_dir,
            self.user_replies_dir,
            self.intersection_dir,
            self.merged_dir,
            self.endpoint_diffs_dir,
            self.state_dir,
            self.reports_dir,
            self.logs_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def create_run_id(self) -> str:
        """Create a Jalali/Tehran timestamped run identifier."""
        return f"run_{_format_jalali(self._tehran_now(), '%Y-%m-%d_%H-%M-%S')}"

    def create_run_report_paths(self, run_id: str) -> Dict[str, Path]:
        safe_run_id = re.sub(r"[^a-zA-Z0-9_\\-]+", "_", str(run_id or self.create_run_id()))
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        return {
            "json": self.reports_dir / f"{safe_run_id}.json",
            "txt": self.reports_dir / f"{safe_run_id}.txt",
        }

    def save_run_report_json(self, report: Dict[str, Any], run_id: str) -> Path:
        paths = self.create_run_report_paths(run_id)
        with paths["json"].open("w", encoding="utf-8") as f:
            json.dump(report if isinstance(report, dict) else {}, f, ensure_ascii=False, indent=2)
        return paths["json"]

    def save_run_report_txt(self, report: Dict[str, Any], run_id: str) -> Path:
        paths = self.create_run_report_paths(run_id)
        summary = report.get("summary", {}) if isinstance(report, dict) else {}
        accounts = report.get("accounts", {}) if isinstance(report, dict) else {}
        lines = [
            f"Run ID: {report.get('run_id', run_id)}",
            f"Started: {report.get('started_at', 'UNKNOWN')}",
            f"Finished: {report.get('finished_at', 'UNKNOWN')}",
            "",
            "Summary",
            f"  Successful endpoints: {summary.get('successful_endpoints', 0)}",
            f"  Partial endpoints: {summary.get('partial_endpoints', 0)}",
            f"  Failed endpoints: {summary.get('failed_endpoints', 0)}",
            f"  Skipped endpoints: {summary.get('skipped_endpoints', 0)}",
            "",
        ]

        for username in sorted(accounts.keys(), key=str.lower):
            account_report = accounts.get(username, {})
            lines.append(f"@{username}")
            if account_report.get("user_id"):
                lines.append(f"  user_id: {account_report['user_id']}")
            if account_report.get("skip_reason"):
                lines.append(f"  skipped: {account_report['skip_reason']}")
            for endpoint, endpoint_report in (account_report.get("endpoints", {}) or {}).items():
                lines.append(
                    "  "
                    f"{endpoint}: status={endpoint_report.get('status')} "
                    f"outcome={endpoint_report.get('outcome')} "
                    f"pages={endpoint_report.get('pages_fetched', 0)} "
                    f"txt_verified={endpoint_report.get('processed_txt_verified', False)}"
                )
                reason = endpoint_report.get("reason")
                if reason:
                    lines.append(f"    reason: {reason}")
            final_sets = account_report.get("final_sets")
            if final_sets:
                lines.append(
                    f"  final_sets: verified={final_sets.get('verified')} "
                    f"counts={final_sets.get('counts', {})}"
                )
            lines.append("")

        with paths["txt"].open("w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
        return paths["txt"]

    # ---------------------------------------------------------------------
    # STATE RECOVERY (cursor persistence)
    # ---------------------------------------------------------------------
    def _read_json_file(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        return {}

    def load_sync_state(self) -> Dict[str, Any]:
        """Load sync state from subsystem path, fallback to legacy paths."""
        data = self._read_json_file(self.sync_state_file)
        if data:
            return data
        global_data = self._read_json_file(self.global_state_dir / "sync_state.json")
        if global_data:
            return global_data
        return self._read_json_file(self.legacy_sync_state_file)

    def save_sync_state(self, state: Dict[str, Any]) -> Path:
        """Persist sync state to subsystem path only."""
        payload = state if isinstance(state, dict) else {}
        self.sync_state_file.parent.mkdir(parents=True, exist_ok=True)
        with self.sync_state_file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return self.sync_state_file

    def get_endpoint_state(self, username: str, endpoint: str) -> Dict[str, Any]:
        """
        Return endpoint sync status for a user.
        Default structure:
          {"last_cursor": None, "status": "pending"}
        """
        uname = self._normalize_username(username)
        state = self.load_sync_state()
        user_state = state.get(uname, {}) if isinstance(state.get(uname, {}), dict) else {}
        ep_state = user_state.get(endpoint, {}) if isinstance(user_state.get(endpoint, {}), dict) else {}
        return {
            "last_cursor": ep_state.get("last_cursor"),
            "status": str(ep_state.get("status", "pending")),
            **ep_state,
        }

    def update_endpoint_state(
        self,
        username: str,
        endpoint: str,
        *,
        last_cursor: Optional[str] = None,
        status: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Path:
        """
        Update one endpoint state atomically inside sync_state.json.
        """
        uname = self._normalize_username(username)
        state = self.load_sync_state()
        if uname not in state or not isinstance(state.get(uname), dict):
            state[uname] = {}
        if endpoint not in state[uname] or not isinstance(state[uname].get(endpoint), dict):
            state[uname][endpoint] = {"last_cursor": None, "status": "pending"}

        if last_cursor is not None:
            state[uname][endpoint]["last_cursor"] = last_cursor
        if status is not None:
            state[uname][endpoint]["status"] = status
        if meta and isinstance(meta, dict):
            state[uname][endpoint].update(meta)

        return self.save_sync_state(state)

    def get_user_id(self, username: str) -> Optional[str]:
        uname = self._normalize_username(username)
        user_state = self.load_sync_state().get(uname, {})
        value = user_state.get("user_id") if isinstance(user_state, dict) else None
        return str(value) if value else None

    def set_user_id(self, username: str, user_id: str) -> Path:
        uname = self._normalize_username(username)
        state = self.load_sync_state()
        if uname not in state or not isinstance(state.get(uname), dict):
            state[uname] = {}
        state[uname]["user_id"] = str(user_id)
        state[uname].pop("skip_current_run", None)
        state[uname].pop("skip_reason", None)
        state[uname].pop("skip_at", None)
        return self.save_sync_state(state)

    def update_account_state(
        self,
        username: str,
        mutator: Callable[[Dict[str, Any]], None],
    ) -> Path:
        """Apply an arbitrary mutation to one account state."""
        uname = self._normalize_username(username)
        state = self.load_sync_state()
        if uname not in state or not isinstance(state.get(uname), dict):
            state[uname] = {}
        mutator(state[uname])
        return self.save_sync_state(state)

    def mark_account_skipped_for_run(self, username: str, reason: str) -> Path:
        """Mark account as skipped for this invocation and both canonical endpoints."""
        now = datetime.utcnow().isoformat() + "Z"

        def mutate(user_state: Dict[str, Any]) -> None:
            user_state["skip_current_run"] = True
            user_state["skip_reason"] = reason
            user_state["skip_at"] = now
            for endpoint in ["UserTweetsAndReplies", "UserTweets"]:
                endpoint_state = user_state.get(endpoint)
                if not isinstance(endpoint_state, dict):
                    endpoint_state = {}
                    user_state[endpoint] = endpoint_state
                endpoint_state.update({
                    "status": "skipped",
                    "skip_reason": reason,
                    "skipped_at": now,
                })

        return self.update_account_state(username, mutate)

    def ensure_account_state(self, username: str) -> Path:
        """Ensure both endpoints exist in sync state for this account."""
        uname = self._normalize_username(username)
        state = self.load_sync_state()
        if uname not in state or not isinstance(state.get(uname), dict):
            state[uname] = {}
        for endpoint in ["UserTweets", "UserTweetsAndReplies"]:
            if endpoint not in state[uname] or not isinstance(state[uname].get(endpoint), dict):
                state[uname][endpoint] = {"last_cursor": None, "status": "pending"}
        return self.save_sync_state(state)

    @staticmethod
    def _normalize_username(username: str) -> str:
        return (username or "unknown").strip().lstrip("@").lower() or "unknown"

    @staticmethod
    def _safe_timestamp(timestamp: str) -> str:
        raw = (timestamp or "").strip()
        if not raw:
            return datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        safe = raw.replace(":", "-").replace(" ", "_").replace("/", "-")
        return safe

    def _tehran_now(self) -> datetime:
        if self.tz:
            return datetime.now(self.tz)
        return datetime.utcnow()

    def _jalali_batch_name(self, dt: Optional[datetime] = None) -> str:
        current = dt or self._tehran_now()
        return _format_jalali(current, "%Y-%m-%d_%H-%M")

    def create_raw_batch_dir(self, endpoint_name: str, username: str, batch_name: Optional[str] = None) -> Path:
        batch = batch_name or self._jalali_batch_name()
        target = self.raw_root / endpoint_name / self._normalize_username(username) / batch
        target.mkdir(parents=True, exist_ok=True)
        return target

    def save_raw_page(self, batch_dir: Path, page_number: int, payload: Dict[str, Any]) -> Path:
        batch_dir.mkdir(parents=True, exist_ok=True)
        output_file = batch_dir / f"page_{int(page_number)}.json"
        with output_file.open("w", encoding="utf-8") as f:
            json.dump(payload if isinstance(payload, dict) else {}, f, ensure_ascii=False, indent=2)
        return output_file

    def load_raw_pages_from_batch(self, batch_path: Any) -> List[Dict[str, Any]]:
        path = Path(str(batch_path)) if batch_path else Path()
        if not path.exists() or not path.is_dir():
            return []
        pages: List[Dict[str, Any]] = []
        for page_file in sorted(path.glob("page_*.json"), key=lambda p: self._page_sort_key(p.name)):
            try:
                with page_file.open("r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, dict):
                    pages.append(payload)
            except Exception:
                continue
        return pages

    def find_raw_batches(self, endpoint_name: str, username: str, include_legacy: bool = True) -> List[Path]:
        """Find subsystem raw batches, optionally including legacy raw_json batches."""
        roots = [self.raw_root / endpoint_name / self._normalize_username(username)]
        if include_legacy:
            roots.append(self.legacy_raw_root / endpoint_name / self._normalize_username(username))
        batches: List[Path] = []
        for root in roots:
            if root.exists():
                batches.extend(path for path in root.iterdir() if path.is_dir())
        return sorted(batches)

    def load_all_raw_pages(self, endpoint_name: str, username: str, include_legacy: bool = True) -> List[Dict[str, Any]]:
        """Load all raw pages for an account/endpoint across known batches."""
        pages: List[Dict[str, Any]] = []
        for batch_dir in self.find_raw_batches(endpoint_name, username, include_legacy=include_legacy):
            pages.extend(self.load_raw_pages_from_batch(batch_dir))
        return pages

    def migrate_legacy_historical_data(self, verify: bool = True) -> Dict[str, Any]:
        """
        Copy legacy v4 data into data/historical and leave legacy files untouched.
        """
        if self.subsystem != "historical":
            return {"status": "skipped", "reason": "only_historical_storage_migrates_legacy"}

        mappings = [
            (self.legacy_raw_root, self.raw_root),
            (self.legacy_processed_root, self.processed_root),
            (self.legacy_reports_dir, self.reports_dir),
            (self.global_state_dir / "sync_state.json", self.sync_state_file),
            (self.global_state_dir / "endpoint_health.json", self.state_dir / "endpoint_health.json"),
        ]
        copied = 0
        verified = 0
        for source, target in mappings:
            if not source.exists():
                continue
            if source.is_dir():
                for item in source.rglob("*"):
                    if not item.is_file():
                        continue
                    destination = target / item.relative_to(source)
                    if destination.exists():
                        if not verify or destination.stat().st_size == item.stat().st_size:
                            verified += 1
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, destination)
                    copied += 1
                    if not verify or destination.stat().st_size == item.stat().st_size:
                        verified += 1
            else:
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    copied += 1
                if not verify or target.stat().st_size == source.stat().st_size:
                    verified += 1
        return {"status": "ok", "copied": copied, "verified": verified}

    @staticmethod
    def _page_sort_key(name: str) -> int:
        match = re.search(r"page_(\d+)\.json$", name)
        return int(match.group(1)) if match else 0

    def save_raw_json(self, data: List[Dict[str, Any]], endpoint_name: str, username: str, timestamp: str) -> Path:
        """
        Save unmodified paginated raw JSON pages.
        If destination exists and already contains a JSON array, append pages.
        """
        endpoint_dir = self.raw_root / endpoint_name / self._normalize_username(username)
        endpoint_dir.mkdir(parents=True, exist_ok=True)

        output_file = endpoint_dir / f"{self._safe_timestamp(timestamp)}.json"
        existing_pages: List[Dict[str, Any]] = []

        if output_file.exists():
            try:
                with output_file.open("r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, list):
                    existing_pages = payload
            except Exception:
                existing_pages = []

        merged_pages = existing_pages + (data if isinstance(data, list) else [])
        with output_file.open("w", encoding="utf-8") as f:
            json.dump(merged_pages, f, ensure_ascii=False, indent=2)
        return output_file

    def save_processed_set(self, data_list: List[Dict[str, Any]], set_name: str, username: str) -> Path:
        """Backward-compatible JSON+TXT writer; canonical v4 uses save_processed_txt_set."""
        normalized = str(set_name).strip()
        folder = self.SET_FOLDER_MAP.get(normalized, self.SET_FOLDER_MAP.get(normalized.upper()))
        if not folder:
            raise ValueError(f"Unsupported set_name: {set_name}")

        target_dir = self.processed_root / folder / self._normalize_username(username)
        target_dir.mkdir(parents=True, exist_ok=True)
        output_file = target_dir / f"{folder}.json"

        with output_file.open("w", encoding="utf-8") as f:
            json.dump(data_list if isinstance(data_list, list) else [], f, ensure_ascii=False, indent=2)
        self.save_processed_txt(data_list if isinstance(data_list, list) else [], output_file.with_suffix(".txt"))
        return output_file

    def save_processed_txt_set(self, data_list: List[Dict[str, Any]], set_name: str, username: str) -> List[Path]:
        """Save canonical v4 processed output as JSON plus v3-style dated TXT files."""
        normalized = str(set_name).strip()
        folder = self.SET_FOLDER_MAP.get(normalized, self.SET_FOLDER_MAP.get(normalized.upper()))
        if not folder:
            raise ValueError(f"Unsupported set_name: {set_name}")

        target_dir = self.processed_root / folder / self._normalize_username(username)
        target_dir.mkdir(parents=True, exist_ok=True)
        output_json = target_dir / f"{folder}.json"
        with output_json.open("w", encoding="utf-8") as f:
            json.dump(data_list if isinstance(data_list, list) else [], f, ensure_ascii=False, indent=2)

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for item in data_list if isinstance(data_list, list) else []:
            date_str = self._processed_item_jalali_date(item)
            grouped.setdefault(date_str, []).append(item)

        if not grouped:
            output_file = target_dir / f"{self._jalali_date(self._tehran_now())}.txt"
            self.save_processed_txt([], output_file)
            return [output_file]

        output_files: List[Path] = []
        for date_str in sorted(grouped.keys()):
            output_file = target_dir / f"{date_str}.txt"
            self.save_processed_txt(grouped[date_str], output_file)
            output_files.append(output_file)
        return output_files

    def _processed_item_jalali_date(self, item: Dict[str, Any]) -> str:
        for key in ("created_at", "raw_timestamp"):
            parsed = self._parse_twitter_timestamp(item.get(key) if isinstance(item, dict) else None)
            if parsed:
                return self._jalali_date(parsed)

        timestamp = str(item.get("timestamp") or "" if isinstance(item, dict) else "").strip()
        match = re.search(r"(\d{4}-\d{2}-\d{2})", timestamp)
        if match:
            return match.group(1)

        return self._jalali_date(self._tehran_now())

    def _parse_twitter_timestamp(self, value: Any) -> Optional[datetime]:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
            return parsed.astimezone(self.tz) if self.tz else parsed
        except Exception:
            return None

    def _jalali_date(self, dt: datetime) -> str:
        return _format_jalali(dt, "%Y-%m-%d")

    def save_processed_txt(self, data_list: List[Dict[str, Any]], output_file: Path) -> Path:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with output_file.open("w", encoding="utf-8") as f:
            f.write(self._format_tweets(data_list or []))
        return output_file

    def _format_tweets(self, tweets: List[Dict[str, Any]]) -> str:
        if not tweets:
            return "No tweets to export.\n"
        return "".join(self._format_item(tweet) for tweet in tweets)

    def _format_count(self, value: Any) -> str:
        """Format numeric count values safely"""
        if value is None or value == "unknown":
            return "UNKNOWN"
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return str(value).upper() if str(value).lower() == "unknown" else str(value)

    def _wrap(self, text: str, width: int = 70) -> str:
        """Wrap long text to readable width."""
        lines = []
        for para in str(text).split("\n"):
            if para:
                lines.append(
                    textwrap.fill(
                        para, width=width, break_long_words=False, break_on_hyphens=False
                    )
                )
            else:
                lines.append("")
        return "\n".join(lines)

    def _format_entities(self, tweet: Dict[str, Any]) -> List[str]:
        """Format URLs, hashtags, mentions, and media from parsed entities."""
        lines: List[str] = []

        entities = tweet.get("entities", {}) or {}
        urls = [entry.get("expanded") or entry.get("short") for entry in entities.get("urls", []) if isinstance(entry, dict) and (entry.get("expanded") or entry.get("short"))]
        if not urls:
            text = str(tweet.get("text") or "")
            urls = sorted(set(re.findall(r"https?://\S+", text)))

        if urls:
            lines.append("🔗 Links:")
            for url in urls:
                lines.append(f"   → {url}")
            lines.append("")

        media_links = entities.get("media_links", [])
        if media_links:
            media_types = entities.get("media_types", [])
            media_type_suffix = f" ({', '.join(media_types)})" if media_types else ""
            lines.append(f"📎 Media: {len(media_links)} item(s){media_type_suffix}")
            for media_url in media_links:
                lines.append(f"   → {media_url}")
            lines.append("")

        hashtags = entities.get("hashtags", [])
        if hashtags:
            lines.append("🏷️  " + " ".join(f"#{tag}" for tag in hashtags))
            lines.append("")

        mentions = entities.get("mentions", [])
        if mentions:
            lines.append("👥 Mentions:")
            for mention in mentions:
                if isinstance(mention, dict):
                    handle = mention.get("handle", "")
                    name = mention.get("name", "")
                    lines.append(f"   @{handle} ({name})".strip())
            lines.append("")

        return lines

    def _metric_line(self, source: Dict[str, Any]) -> str:
        return (
            f"💬 Replies: {self._format_count(source.get('replies'))}  "
            f"🔁 Retweets: {self._format_count(source.get('retweets'))}  "
            f"❤️ Likes: {self._format_count(source.get('likes'))}  "
            f"💭 Quotes: {self._format_count(source.get('quotes'))}  "
            f"🔖 Bookmarks: {self._format_count(source.get('bookmarks'))}  "
            f"👁 Views: {self._format_count(source.get('views'))}"
        )

    def _get_type_icon(self, tweet_type: str) -> str:
        """Return icon for tweet type"""
        mapping = {
            "Tweet": "📝",
            "Retweet": "🔁",
            "Reply": "💬",
            "Quote": "💭",
        }
        return mapping.get(tweet_type, "📝")

    def _format_tweet(self, tweet: Dict[str, Any]) -> str:
        text_payload = choose_export_text(tweet.get("text", ""), tweet.get("source_language"), tweet.get("translation_meta"))
        lines = ["💬 TWEET", ""]
        lines.append(f"👤 @{tweet.get('account', 'unknown')}")
        lines.append(f"📅 {tweet.get('timestamp', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._wrap(text_payload["text"]))
        if text_payload.get("note"):
            lines.append("")
            lines.append(text_payload["note"])
        lines.append("")
        lines.extend(self._format_entities(tweet))
        lines.append(f"🔗 {tweet.get('url', '')}")
        lines.append(f"🆔 Tweet ID: {tweet.get('id', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._metric_line(tweet))
        if "source_endpoint" in tweet:
            lines.extend(["", f"Source Endpoint: {tweet['source_endpoint']}"])
        lines.extend(["", "═" * 70, "═" * 70, ""])
        return "\n".join(lines)

    def _format_retweet(self, tweet: Dict[str, Any]) -> str:
        original_export = choose_export_text(tweet.get("retweeted_text") or tweet.get("text", ""), None, tweet.get("retweeted_translation_meta"))
        lines = ["🔁 RETWEET", ""]
        lines.append(f"👤 Retweeted by: @{tweet.get('account', 'unknown')}")
        lines.append(f"📅 Retweeted at: {tweet.get('timestamp', 'UNKNOWN')}")
        lines.append("")
        lines.append("┌" + "─" * 68 + "┐")
        lines.append("│ ORIGINAL TWEET" + " " * 53 + "│")
        lines.append("├" + "─" * 68 + "┤")

        original_author = tweet.get("retweeted_author")
        original_tweet_id = tweet.get("retweeted_tweet_id")
        original_author_display = f"@{original_author}" if original_author else "UNKNOWN"
        author_line = f"👤 {original_author_display}"
        lines.append(f"│ {author_line}" + " " * max(0, 68 - len(f"│ {author_line}")) + "│")
        lines.append("│" + " " * 68 + "│")

        text = self._wrap(original_export["text"])
        for line in text.split("\n"):
            for wrapped in textwrap.wrap(line, width=66) if line else [""]:
                lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
        if original_export.get("note"):
            lines.append("│" + " " * 68 + "│")
            note_text = original_export["note"]
            for wrapped in textwrap.wrap(note_text, width=66):
                lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
        lines.append("│" + " " * 68 + "│")

        if tweet.get("retweeted_timestamp"):
            time_line = f"📅 {tweet['retweeted_timestamp']}"
            lines.append(f"│ {time_line}" + " " * max(0, 68 - len(f"│ {time_line}")) + "│")
            lines.append("│" + " " * 68 + "│")

        if original_tweet_id:
            original_url = f"https://x.com/{original_author or 'i'}/status/{original_tweet_id}"
            link_line = f"🔗 {original_url}"
            id_line = f"🆔 Tweet ID: {original_tweet_id}"
            lines.append(f"│ {link_line}" + " " * max(0, 68 - len(f"│ {link_line}")) + "│")
            lines.append(f"│ {id_line}" + " " * max(0, 68 - len(f"│ {id_line}")) + "│")
        else:
            lines.append("│ 🔗 UNKNOWN" + " " * 57 + "│")
            lines.append("│ 🆔 Tweet ID: UNKNOWN" + " " * 46 + "│")

        engagement_line = self._metric_line(tweet)
        lines.append("│" + " " * 68 + "│")
        lines.append(f"│ {engagement_line}" + " " * max(0, 68 - len(f"│ {engagement_line}")) + "│")
        lines.append("└" + "─" * 68 + "┘")

        if "source_endpoint" in tweet:
            lines.extend(["", f"Source Endpoint: {tweet['source_endpoint']}"])
        lines.extend(["", "═" * 70, "═" * 70, ""])
        return "\n".join(lines)

    def _format_reply(self, tweet: Dict[str, Any]) -> str:
        reply_text_payload = choose_export_text(tweet.get("text", ""), tweet.get("source_language"), tweet.get("translation_meta"))
        lines = ["↩️ REPLY", ""]
        lines.append(f"👤 Reply by: @{tweet.get('account', 'unknown')}")
        lines.append(f"📅 {tweet.get('timestamp', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._wrap(reply_text_payload["text"]))
        if reply_text_payload.get("note"):
            lines.append("")
            lines.append(reply_text_payload["note"])
        lines.append("")
        lines.extend(self._format_entities(tweet))
        lines.append(f"🔗 {tweet.get('url', '')}")
        lines.append(f"🆔 Tweet ID: {tweet.get('id', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._metric_line(tweet))
        lines.append("")

        parent_id = tweet.get("in_reply_to_status_id")
        parent_screen = tweet.get("in_reply_to_screen_name")
        parent_tweet = tweet.get("parent_tweet", {}) or {}
        chain = tweet.get("conversation_chain", []) or []
        if chain:
            lines.append("┌" + "─" * 68 + "┐")
            lines.append("│ CONVERSATION THREAD" + " " * 48 + "│")
            lines.append("├" + "─" * 68 + "┤")
            for idx, node in enumerate(chain, start=1):
                role = "Direct parent" if idx == len(chain) else f"Ancestor {idx}"
                author_line = f"{role}: @{node.get('account', 'unknown')}"
                lines.append(f"│ {author_line}" + " " * max(0, 68 - len(f"│ {author_line}")) + "│")
                if node.get("timestamp"):
                    time_line = f"📅 {node['timestamp']}"
                    lines.append(f"│ {time_line}" + " " * max(0, 68 - len(f"│ {time_line}")) + "│")
                node_text_payload = choose_export_text(
                    node.get("text", ""),
                    node.get("source_language"),
                    node.get("translation_meta"),
                )
                text_value = self._wrap(node_text_payload["text"])
                for line in text_value.split("\n"):
                    for wrapped in textwrap.wrap(line, width=66) if line else [""]:
                        lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
                if node_text_payload.get("note"):
                    lines.append("│" + " " * 68 + "│")
                    for wrapped in textwrap.wrap(node_text_payload["note"], width=66):
                        lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
                if node.get("url"):
                    link_line = f"🔗 {node['url']}"
                    lines.append(f"│ {link_line}" + " " * max(0, 68 - len(f"│ {link_line}")) + "│")
                if node.get("id"):
                    id_line = f"🆔 Tweet ID: {node['id']}"
                    lines.append(f"│ {id_line}" + " " * max(0, 68 - len(f"│ {id_line}")) + "│")
                if idx != len(chain):
                    lines.append("│" + " " * 68 + "│")
                    lines.append("│ replies to" + " " * 58 + "│")
                    lines.append("│" + " " * 68 + "│")
            lines.append("└" + "─" * 68 + "┘")
            lines.append("")

        lines.append("┌" + "─" * 68 + "┐")
        lines.append("│ IN REPLY TO" + " " * 56 + "│")
        lines.append("├" + "─" * 68 + "┤")
        parent_label = f"@{parent_screen}" if parent_screen else "UNKNOWN"
        if parent_tweet:
            parent_label = f"@{parent_tweet.get('account', parent_label).lstrip('@')}"
        label_line = f"👤 {parent_label}"
        lines.append(f"│ {label_line}" + " " * max(0, 68 - len(f"│ {label_line}")) + "│")
        if tweet.get("conversation_id"):
            conv_line = f"🧵 Conversation ID: {tweet['conversation_id']}"
            lines.append(f"│ {conv_line}" + " " * max(0, 68 - len(f"│ {conv_line}")) + "│")
        lines.append("│" + " " * 68 + "│")

        if parent_tweet.get("text"):
            parent_text_payload = choose_export_text(
                parent_tweet.get("text", ""),
                parent_tweet.get("source_language"),
                parent_tweet.get("translation_meta"),
            )
            parent_text = self._wrap(parent_text_payload["text"])
            for line in parent_text.split("\n"):
                for wrapped in textwrap.wrap(line, width=66) if line else [""]:
                    lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
            if parent_text_payload.get("note"):
                lines.append("│" + " " * 68 + "│")
                for wrapped in textwrap.wrap(parent_text_payload["note"], width=66):
                    lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
            lines.append("│" + " " * 68 + "│")

        if parent_id:
            parent_url = parent_tweet.get("url") or f"https://x.com/{parent_screen or 'i'}/status/{parent_id}"
            link_line = f"🔗 {parent_url}"
            id_line = f"🆔 Tweet ID: {parent_id}"
            lines.append(f"│ {link_line}" + " " * max(0, 68 - len(f"│ {link_line}")) + "│")
            lines.append(f"│ {id_line}" + " " * max(0, 68 - len(f"│ {id_line}")) + "│")
        else:
            lines.append("│ 🔗 UNKNOWN" + " " * 57 + "│")
            lines.append("│ 🆔 Tweet ID: UNKNOWN" + " " * 46 + "│")
        lines.append("└" + "─" * 68 + "┘")

        if "source_endpoint" in tweet:
            lines.extend(["", f"Source Endpoint: {tweet['source_endpoint']}"])
        lines.extend(["", "═" * 70, "═" * 70, ""])
        return "\n".join(lines)

    def _format_quote(self, tweet: Dict[str, Any]) -> str:
        quote_text_payload = choose_export_text(tweet.get("text", ""), tweet.get("source_language"), tweet.get("translation_meta"))
        lines = ["📎 QUOTE TWEET", ""]
        lines.append(f"👤 Quote by: @{tweet.get('account', 'unknown')}")
        lines.append(f"📅 {tweet.get('timestamp', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._wrap(quote_text_payload["text"]))
        if quote_text_payload.get("note"):
            lines.append("")
            lines.append(quote_text_payload["note"])
        lines.append("")
        lines.extend(self._format_entities(tweet))
        lines.append(f"🔗 {tweet.get('url', '')}")
        lines.append(f"🆔 Tweet ID: {tweet.get('id', 'UNKNOWN')}")
        lines.append("")
        lines.append(self._metric_line(tweet))
        lines.append("")
        lines.append("┌" + "─" * 68 + "┐")
        lines.append("│ QUOTED TWEET" + " " * 55 + "│")
        lines.append("├" + "─" * 68 + "┤")
        quoted_author = tweet.get("quoted_author")
        quoted_id = tweet.get("quoted_tweet_id")
        if quoted_author:
            author_line = f"👤 @{quoted_author}"
            lines.append(f"│ {author_line}" + " " * max(0, 68 - len(f"│ {author_line}")) + "│")
        else:
            lines.append("│ 👤 UNKNOWN" + " " * 57 + "│")
        lines.append("│" + " " * 68 + "│")

        if tweet.get("quoted_timestamp"):
            time_line = f"📅 {tweet['quoted_timestamp']}"
            lines.append(f"│ {time_line}" + " " * max(0, 68 - len(f"│ {time_line}")) + "│")
            lines.append("│" + " " * 68 + "│")

        if tweet.get("quoted_text"):
            quoted_text_payload = choose_export_text(
                tweet.get("quoted_text", ""),
                None,
                tweet.get("quoted_translation_meta"),
            )
            quoted_text = self._wrap(quoted_text_payload["text"])
            for line in quoted_text.split("\n"):
                for wrapped in textwrap.wrap(line, width=66) if line else [""]:
                    lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
            if quoted_text_payload.get("note"):
                lines.append("│" + " " * 68 + "│")
                for wrapped in textwrap.wrap(quoted_text_payload["note"], width=66):
                    lines.append(f"│ {wrapped}" + " " * max(0, 68 - len(f"│ {wrapped}")) + "│")
            lines.append("│" + " " * 68 + "│")

        if quoted_id:
            quoted_url = f"https://x.com/{quoted_author or 'i'}/status/{quoted_id}"
            link_line = f"🔗 {quoted_url}"
            id_line = f"🆔 Tweet ID: {quoted_id}"
            lines.append(f"│ {link_line}" + " " * max(0, 68 - len(f"│ {link_line}")) + "│")
            lines.append(f"│ {id_line}" + " " * max(0, 68 - len(f"│ {id_line}")) + "│")
        else:
            lines.append("│ 🔗 UNKNOWN" + " " * 57 + "│")
            lines.append("│ 🆔 Tweet ID: UNKNOWN" + " " * 46 + "│")
        lines.append("└" + "─" * 68 + "┘")
        if "source_endpoint" in tweet:
            lines.extend(["", f"Source Endpoint: {tweet['source_endpoint']}"])
        lines.extend(["", "═" * 70, "═" * 70, ""])
        return "\n".join(lines)

    def _format_item(self, tweet: Dict[str, Any]) -> str:
        tweet_type = str(tweet.get("type") or "Tweet").lower()
        if tweet_type == "retweet":
            return self._format_retweet(tweet)
        if tweet_type == "reply":
            return self._format_reply(tweet)
        if tweet_type == "quote":
            return self._format_quote(tweet)
        return self._format_tweet(tweet)

    def save_tweets_to_file(self, tweets: List[Dict[str, Any]], account: str, date_str: str, output_dir: Path) -> int:
        """
        Compatibility writer for endpoint text snapshots used by fetcher engine.
        """
        account_dir = output_dir / self._normalize_username(account)
        account_dir.mkdir(parents=True, exist_ok=True)
        output_file = account_dir / f"{date_str}.txt"

        rows: List[str] = []
        for tweet in tweets or []:
            text = (
                (tweet.get("legacy", {}) if isinstance(tweet.get("legacy", {}), dict) else {}).get("full_text")
                or tweet.get("text")
                or ""
            )
            tweet_id = tweet.get("rest_id") or tweet.get("id") or "unknown"
            timestamp = (
                (tweet.get("legacy", {}) if isinstance(tweet.get("legacy", {}), dict) else {}).get("created_at")
                or tweet.get("timestamp")
                or "UNKNOWN"
            )
            rows.append(f"[{timestamp}] {tweet_id}\n{text}\n{'-' * 40}\n")

        with output_file.open("w", encoding="utf-8") as f:
            f.write("\n".join(rows) if rows else "No tweets.\n")
        return len(tweets or [])

    def log_event(self, log_type: str, message: str) -> Path:
        """Compatibility event logger used by older modules."""
        safe_name = re.sub(r"[^a-zA-Z0-9_\\-]+", "_", log_type or "events")
        log_file = self.logs_dir / f"{safe_name}.log"
        line = f"{datetime.utcnow().isoformat()}Z | {message}\n"
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line)
        return log_file

    @staticmethod
    def get_jalali_date(dt: Optional[datetime] = None) -> str:
        """Compatibility date helper with Jalali output when available."""
        target = dt or datetime.utcnow()
        return _format_jalali(target, "%Y-%m-%d")

    @staticmethod
    def get_jalali_datetime(dt: Optional[datetime] = None) -> str:
        """Compatibility datetime helper with Jalali output when available."""
        target = dt or datetime.utcnow()
        return _format_jalali(target, "%Y-%m-%d %H:%M:%S")
