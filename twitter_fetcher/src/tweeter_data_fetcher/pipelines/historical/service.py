#!/usr/bin/env python3
"""Historical profile-timeline backfill runner.

Fetches UserTweets (and optionally UserTweetsAndReplies) for one or more
accounts, then builds the seven processed set-algebra folders per account under
``data/historical_live/`` and writes a per-run report.

Run:
    tdf-historical --only elonmusk
    python -m tweeter_data_fetcher.pipelines.historical.service --only elonmusk

Flags:
    --only <user>              account to fetch (repeatable / comma-separated)
    --only-account <user>      alias for --only
    --no-user-tweets           skip UserTweets (on by default)
    --no-with-replies          skip UserTweetsAndReplies (on by default)
    --validation-run-id <id>   isolate output under data/validation/<id>/
"""
from __future__ import annotations


import sys
import argparse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# src/ must be importable when this module is run directly as a script.
_SRC_ROOT = Path(__file__).resolve().parents[3]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from tweeter_data_fetcher.paths import PROJECT_ROOT

from tweeter_data_fetcher.configuration import get_priority_policy, ordered_accounts
from tweeter_data_fetcher.x_api.timeline import FetcherEngine
from tweeter_data_fetcher.processing.sets import TweetSetProcessor
from tweeter_data_fetcher.processing.windows import RollingWindowEvaluator, window_cutoff
from tweeter_data_fetcher.storage.facade import StorageManager
from tweeter_data_fetcher.observability.logging_setup import attach_run_id
from tweeter_data_fetcher.observability.pipeline_console import PipelineConsole


ENDPOINTS = ("UserTweets", "UserTweetsAndReplies")
CONSOLE = PipelineConsole("historical")


# Raw page loading and verification -----------------------------------------


def _endpoint_pages(storage: StorageManager, username: str, endpoint: str) -> List[Dict[str, Any]]:
    state = storage.get_endpoint_state(username, endpoint)
    raw_batch_path = state.get("raw_batch_path")
    if not raw_batch_path:
        return storage.load_all_raw_pages(endpoint, username, include_legacy=True)
    return storage.load_raw_pages_from_batch(raw_batch_path)


def _endpoint_raw_batch_path(storage: StorageManager, username: str, endpoint: str) -> Optional[Path]:
    state = storage.get_endpoint_state(username, endpoint)
    raw_batch_path = state.get("raw_batch_path")
    if not raw_batch_path:
        return None
    path = Path(str(raw_batch_path))
    return path if path.exists() and path.is_dir() else None


def _historical_window_days_for(engine: FetcherEngine, username: str) -> int:
    policy = get_priority_policy(username, engine.account_map, engine.priority_policies)
    return int(policy.get("historical_window_days", 1))


def _endpoint_window_coverage(
    *,
    evaluator: RollingWindowEvaluator,
    storage: StorageManager,
    username: str,
    endpoint: str,
    window_days: int,
) -> Tuple[bool, Dict[str, Any], List[Dict[str, Any]]]:
    raw_pages = storage.load_all_raw_pages(endpoint, username, include_legacy=True)
    coverage = evaluator.evaluate_raw_pages(raw_pages, username, endpoint, window_days)
    return coverage.complete, coverage.__dict__, raw_pages


def _verify_raw_pages(
    *,
    storage: StorageManager,
    username: str,
    endpoint: str,
    raw_pages: List[Dict[str, Any]],
) -> bool:
    batch_path = _endpoint_raw_batch_path(storage, username, endpoint)
    page_files = sorted(batch_path.glob("page_*.json")) if batch_path else []
    ok = bool(raw_pages) and bool(page_files) and len(page_files) >= len(raw_pages)
    if ok:
        CONSOLE.success(f"@{username} {endpoint} raw verified: {len(page_files)} page file(s)")
    else:
        CONSOLE.warning(
            f"@{username} {endpoint} raw verification: "
            f"loaded_pages={len(raw_pages)} page_files={len(page_files)}"
        )
    return ok


def _verify_txt_files(username: str, label: str, paths: List[Path]) -> bool:
    missing_or_empty = [path for path in paths if not path.exists() or path.stat().st_size == 0]
    if not paths or missing_or_empty:
        CONSOLE.warning(
            f"@{username} {label} TXT verification: "
            f"files={len(paths)} missing_or_empty={len(missing_or_empty)}"
        )
        return False
    CONSOLE.success(f"@{username} {label} TXT verified: {len(paths)} file(s)")
    return True


def _process_account(
    *,
    storage: StorageManager,
    processor: TweetSetProcessor,
    username: str,
    raw_pages_by_endpoint: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Tuple[bool, Dict[str, int]]:
    raw_pages_by_endpoint = raw_pages_by_endpoint or {}
    raw_replies = raw_pages_by_endpoint.get("UserTweetsAndReplies") or _endpoint_pages(
        storage, username, "UserTweetsAndReplies"
    )
    raw_tweets = raw_pages_by_endpoint.get("UserTweets") or _endpoint_pages(
        storage, username, "UserTweets"
    )

    set_a = processor.extract_tweets_from_raw(
        raw_tweets,
        username=username,
        source_endpoint="UserTweets",
    )
    set_b = processor.extract_tweets_from_raw(
        raw_replies,
        username=username,
        source_endpoint="UserTweetsAndReplies",
    )

    list_a = list(set_a.values())
    list_b = list(set_b.values())
    list_intersection = processor.get_intersection(set_a, set_b)
    list_union = processor.get_union(set_a, set_b)
    list_a_minus_b = processor.get_difference_a_minus_b(set_a, set_b)
    list_b_minus_a = processor.get_difference_b_minus_a(set_a, set_b)
    list_symmetric_difference = processor.get_symmetric_difference(set_a, set_b)

    txt_outputs = {
        "1_user_tweets": storage.save_processed_set_merged(list_a, "1_user_tweets", username),
        "2_user_tweets_and_replies": storage.save_processed_set_merged(list_b, "2_user_tweets_and_replies", username),
        "3_intersection": storage.save_processed_set_merged(list_intersection, "3_intersection", username),
        "4_union": storage.save_processed_set_merged(list_union, "4_union", username),
        "5_a_minus_b": storage.save_processed_set_merged(list_a_minus_b, "5_a_minus_b", username),
        "6_b_minus_a": storage.save_processed_set_merged(list_b_minus_a, "6_b_minus_a", username),
        "7_symmetric_difference": storage.save_processed_set_merged(list_symmetric_difference, "7_symmetric_difference", username),
    }
    verified = all(_verify_txt_files(username, set_name, paths) for set_name, paths in txt_outputs.items())

    CONSOLE.success(
        f"@{username} processed | "
        f"tweets={len(list_a)} replies_endpoint={len(list_b)} "
        f"intersection={len(list_intersection)} union={len(list_union)} "
        f"a_minus_b={len(list_a_minus_b)} b_minus_a={len(list_b_minus_a)} "
        f"symmetric_difference={len(list_symmetric_difference)}"
    )
    return verified, {
        "tweets": len(list_a),
        "replies_endpoint": len(list_b),
        "intersection": len(list_intersection),
        "union": len(list_union),
        "a_minus_b": len(list_a_minus_b),
        "b_minus_a": len(list_b_minus_a),
        "symmetric_difference": len(list_symmetric_difference),
    }


# Processed output and reporting --------------------------------------------


def _save_endpoint_processed_txt(
    *,
    storage: StorageManager,
    processor: TweetSetProcessor,
    username: str,
    endpoint: str,
    raw_pages: List[Dict[str, Any]],
) -> bool:
    if not raw_pages:
        raw_pages = _endpoint_pages(storage, username, endpoint)
    set_name = "2_user_tweets_and_replies" if endpoint == "UserTweetsAndReplies" else "1_user_tweets"
    extracted = processor.extract_tweets_from_raw(
        raw_pages,
        username=username,
        source_endpoint=endpoint,
    )
    paths = storage.save_processed_set_merged(list(extracted.values()), set_name, username)
    CONSOLE.info(f"@{username} {endpoint} TXT updated: {len(extracted)} item(s)")
    raw_ok = _verify_raw_pages(
        storage=storage,
        username=username,
        endpoint=endpoint,
        raw_pages=raw_pages,
    )
    txt_ok = _verify_txt_files(username, set_name, paths)
    return raw_ok and txt_ok


def _safe_endpoint_report(result: Dict[str, Any]) -> Dict[str, Any]:
    status = result.get("status")
    transport = result.get("transport")
    endpoint_status = (
        "verified_browser_fallback"
        if status == "completed" and transport == "browser_fallback"
        else ("verified_http" if status == "completed" else ("unverified" if status in {"partial", "skipped"} else "failed"))
    )
    return {
        "status": result.get("status"),
        "endpoint_status": endpoint_status,
        "outcome": result.get("outcome"),
        "reason": result.get("reason"),
        "pages_fetched": result.get("pages_fetched", 0),
        "raw_batch_path": result.get("raw_batch_path"),
        "last_cursor": result.get("last_cursor"),
        "cursor_termination_reason": result.get("cursor_termination_reason"),
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


def _update_report_summary(report: Dict[str, Any]) -> None:
    summary = {
        "successful_endpoints": 0,
        "partial_endpoints": 0,
        "failed_endpoints": 0,
        "skipped_endpoints": 0,
        "txt_unverified_endpoints": 0,
    }
    for account_report in report.get("accounts", {}).values():
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
    report["summary"] = summary


def _print_report_summary(report: Dict[str, Any], json_path: Path, txt_path: Path) -> None:
    _update_report_summary(report)
    CONSOLE.banner("Run summary")
    for key, value in report.get("summary", {}).items():
        CONSOLE.info(f"{key}: {value}")

    grouped: Dict[str, List[str]] = {"failed": [], "partial": [], "skipped": []}
    for username, account_report in report.get("accounts", {}).items():
        for endpoint, endpoint_report in (account_report.get("endpoints", {}) or {}).items():
            status = endpoint_report.get("status")
            if status in grouped:
                grouped[status].append(f"@{username} {endpoint} ({endpoint_report.get('outcome')})")
    for status, items in grouped.items():
        if items:
            CONSOLE.warning(f"{status}: {', '.join(items)}")
    CONSOLE.info(f"Report JSON: {json_path}")
    CONSOLE.info(f"Report TXT: {txt_path}")


def _disabled_endpoint_result(username: str, endpoint: str) -> Dict[str, Any]:
    return {
        "account": username,
        "endpoint": endpoint,
        "status": "skipped",
        "outcome": "skipped_disabled_by_cli",
        "reason": "Endpoint disabled by historical runner toggle",
        "pages": [],
        "pages_fetched": 0,
        "raw_batch_path": "",
        "last_cursor": None,
        "last_http_status": None,
        "attempts": 0,
        "error_samples": [],
        "started_at": datetime.utcnow().isoformat() + "Z",
        "finished_at": datetime.utcnow().isoformat() + "Z",
        "window_coverage": None,
    }


def _fetch_or_skip_endpoint(
    *,
    engine: FetcherEngine,
    evaluator: RollingWindowEvaluator,
    storage: StorageManager,
    username: str,
    user_id: str,
    endpoint: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    window_days = _historical_window_days_for(engine, username)
    _, window_coverage, _ = _endpoint_window_coverage(
        evaluator=evaluator,
        storage=storage,
        username=username,
        endpoint=endpoint,
        window_days=window_days,
    )

    watermark = storage.get_fetch_watermark(username, endpoint)
    cutoff = window_cutoff(window_days=window_days, watermark=watermark, floor="day")

    return engine._fetch_endpoint_result(
        account=username,
        user_id=user_id,
        endpoint=endpoint,
        max_pages=engine.pagination_safety_cap_pages,
        window_days=window_days,
        cutoff=cutoff,
        force_refetch=True,
    ), window_coverage


def run_v4(
    selected_accounts: Optional[List[str]] = None,
    *,
    enable_user_tweets: bool = True,
    enable_user_tweets_and_replies: bool = True,
    validation_run_id: Optional[str] = None,
) -> None:
    project_root = PROJECT_ROOT
    engine = FetcherEngine(validation_run_id=validation_run_id)
    storage = StorageManager(
        project_root=project_root,
        subsystem="historical_live",
        data_root_override=engine.data_root,
    )
    processor = TweetSetProcessor()
    evaluator = RollingWindowEvaluator()
    console = engine.logger
    migration_report = {"status": "skipped_validation"} if validation_run_id else storage.migrate_legacy_historical_data(verify=True)
    console.info(f"Historical storage migration: {migration_report}")

    accounts = ordered_accounts(engine.account_map) if selected_accounts is None else selected_accounts
    if not accounts:
        console.warning("No accounts found in tier configuration.")
        return

    accounts = [account.strip().lstrip("@") for account in accounts if account and account.strip()]
    run_id = storage.create_run_id()
    # Stamp the run id on every subsequent log record + JSONL event so the
    # file log, console bridge, and events.jsonl are greppable as one run.
    attach_run_id(run_id)
    engine.recorder.run_id = run_id
    engine.recorder.emit("run_start", accounts=accounts, validation_run_id=validation_run_id)
    report: Dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.utcnow().isoformat() + "Z",
        "config": {
            "endpoint_order": ["UserTweets", "UserTweetsAndReplies"],
            "enabled_endpoints": [
                endpoint
                for endpoint, enabled in (
                    ("UserTweets", enable_user_tweets),
                    ("UserTweetsAndReplies", enable_user_tweets_and_replies),
                )
                if enabled
            ],
            "accounts_requested": len(accounts),
            "completion_rule": "tehran_jalali_rolling_window",
            "pagination_safety_cap_pages": engine.pagination_safety_cap_pages,
            "first_request_warmup_seconds": engine.first_request_warmup_seconds,
            "historical_storage_migration": migration_report,
            "validation_run_id": validation_run_id,
            "data_root": str(engine.data_root),
        },
        "summary": {},
        "accounts": {},
    }
    user_ids: Dict[str, str] = {}
    active_accounts: List[str] = []
    fetched_pages: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        username: {} for username in accounts
    }

    console.phase_banner("Resolve user IDs", pass_index=1, pass_total=4, account_count=len(accounts))
    engine.recorder.emit_phase_start(phase="resolve_user_ids", accounts=len(accounts), pass_index=1, pass_total=4)
    for username in accounts:
        storage.ensure_account_state(username)
        report["accounts"].setdefault(username, {"endpoints": {}})
        try:
            bootstrap = engine.bootstrap_browser_context(username=username)
            report["accounts"][username]["browser_bootstrap"] = {
                "ok": bootstrap.ok,
                "route": bootstrap.route,
                "support_request_count": bootstrap.support_request_count,
                "error": bootstrap.error,
            }
            user_ids[username] = engine._get_user_id(username)
        except Exception as exc:
            reason = f"UserByScreenName failed: {exc}"
            storage.mark_account_skipped_for_run(username, reason)
            report["accounts"][username]["skip_reason"] = reason
            for endpoint in ENDPOINTS:
                report["accounts"][username]["endpoints"][endpoint] = {
                    "status": "skipped",
                    "outcome": "skipped_user_id",
                    "reason": reason,
                    "pages_fetched": 0,
                    "processed_txt_verified": None,
                }
            console.error(f"@{username} skipped for this run: {reason}")
            continue
        active_accounts.append(username)
        report["accounts"][username]["user_id"] = user_ids[username]
        console.success(f"@{username} -> user_id={user_ids[username]}")

    if not active_accounts:
        console.warning("No accounts with resolved user IDs; nothing to fetch.")
        report["finished_at"] = datetime.utcnow().isoformat() + "Z"
        _update_report_summary(report)
        json_path = storage.save_run_report_json(report, run_id)
        txt_path = storage.save_run_report_txt(report, run_id)
        _print_report_summary(report, json_path, txt_path)
        engine.recorder.emit("run_end", summary=report["summary"], report_json=str(json_path))
        return

    enabled_endpoints = [
        endpoint
        for endpoint, enabled in (
            ("UserTweets", enable_user_tweets),
            ("UserTweetsAndReplies", enable_user_tweets_and_replies),
        )
        if enabled
    ]
    disabled_endpoints = [endpoint for endpoint in ENDPOINTS if endpoint not in enabled_endpoints]

    for endpoint in disabled_endpoints:
        for username in active_accounts:
            result = _disabled_endpoint_result(username, endpoint)
            report["accounts"][username]["endpoints"][endpoint] = _safe_endpoint_report(result)

    console.phase_banner("Global endpoint fetching", pass_index=2, pass_total=4, account_count=len(active_accounts))
    for endpoint_index, endpoint in enumerate(enabled_endpoints, start=1):
        engine.recorder.emit_phase_start(
            phase="fetch_endpoint",
            endpoint=endpoint,
            accounts=len(active_accounts),
            pass_index=endpoint_index,
            pass_total=len(enabled_endpoints),
        )
        for idx, username in enumerate(active_accounts):
            console.info(f"@{username}: fetching {endpoint}")
            result, window_coverage = _fetch_or_skip_endpoint(
                engine=engine,
                evaluator=evaluator,
                storage=storage,
                username=username,
                user_id=user_ids[username],
                endpoint=endpoint,
            )
            fetched_pages[username][endpoint] = result.get("pages", [])
            endpoint_verified = _save_endpoint_processed_txt(
                storage=storage,
                processor=processor,
                username=username,
                endpoint=endpoint,
                raw_pages=fetched_pages[username][endpoint],
            )
            endpoint_report = _safe_endpoint_report(result)
            endpoint_report["processed_txt_verified"] = endpoint_verified
            report["accounts"][username]["endpoints"][endpoint] = endpoint_report
            storage.update_endpoint_state(
                username,
                endpoint,
                meta={
                    "processed_txt_verified": endpoint_verified,
                    "run_id": run_id,
                    "outcome": result.get("outcome"),
                    "pages_fetched": result.get("pages_fetched", 0),
                    "window_coverage": result.get("window_coverage") or window_coverage,
                },
            )
            if idx < len(active_accounts) - 1:
                engine.api_manager.human_delay("between_accounts")

    console.phase_banner("Generate processed sets", pass_index=4, pass_total=4, account_count=len(active_accounts))
    engine.recorder.emit_phase_start(phase="generate_processed_sets", accounts=len(active_accounts), pass_index=4, pass_total=4)
    for username in active_accounts:
        final_verified, counts = _process_account(
            storage=storage,
            processor=processor,
            username=username,
            raw_pages_by_endpoint=fetched_pages.get(username),
        )
        report["accounts"][username]["final_sets"] = {
            "verified": final_verified,
            "counts": counts,
        }
        storage.update_account_state(
            username,
            lambda state, verified=final_verified, count_data=counts, current_run_id=run_id: state.update({
                "processed_sets_verified": verified,
                "processed_counts": count_data,
                "last_run_id": current_run_id,
            }),
        )

    report["finished_at"] = datetime.utcnow().isoformat() + "Z"
    _update_report_summary(report)
    json_path = storage.save_run_report_json(report, run_id)
    txt_path = storage.save_run_report_txt(report, run_id)
    _print_report_summary(report, json_path, txt_path)
    engine.recorder.emit("run_end", summary=report["summary"], report_json=str(json_path))
    console.success("Browser-order pipeline complete.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the historical v4 fetch pipeline.")
    parser.add_argument("--only", action="append", default=[], help="Account username to fetch; may be repeated or comma-separated.")
    parser.add_argument("--only-account", action="append", default=[], help="Alias for --only.")
    parser.add_argument("--user-tweets", dest="enable_user_tweets", action="store_true", default=True)
    parser.add_argument("--no-user-tweets", dest="enable_user_tweets", action="store_false")
    parser.add_argument("--with-replies", dest="enable_user_tweets_and_replies", action="store_true", default=True)
    parser.add_argument("--no-with-replies", dest="enable_user_tweets_and_replies", action="store_false")
    parser.add_argument("--validation-run-id", help="Write isolated output under data/validation/<run_id>/ and bypass stale skip state.")
    return parser.parse_args()


def _selected_accounts_from_args(args: argparse.Namespace) -> Optional[List[str]]:
    values = list(args.only or []) + list(args.only_account or [])
    selected: List[str] = []
    for value in values:
        selected.extend(part.strip().lstrip("@") for part in str(value).split(",") if part.strip())
    return selected or None


def main() -> None:
    args = _parse_args()
    run_v4(
        selected_accounts=_selected_accounts_from_args(args),
        enable_user_tweets=args.enable_user_tweets,
        enable_user_tweets_and_replies=args.enable_user_tweets_and_replies,
        validation_run_id=args.validation_run_id,
    )


if __name__ == "__main__":
    main()
