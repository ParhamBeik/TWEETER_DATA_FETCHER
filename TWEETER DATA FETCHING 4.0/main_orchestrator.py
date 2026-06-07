#!/usr/bin/env python3
"""Canonical v4 replies-first fetch and processing pipeline."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config.tier_config import get_priority_policy, ordered_accounts
from core.fetcher_engine import DEFAULT_HISTORICAL_MAX_PAGES, FetcherEngine
from core.set_operations import TweetSetProcessor
from data_pipeline.storage_manager import StorageManager


ENDPOINTS = ("UserTweetsAndReplies", "UserTweets")


def _endpoint_pages(storage: StorageManager, username: str, endpoint: str) -> List[Dict[str, Any]]:
    state = storage.get_endpoint_state(username, endpoint)
    raw_batch_path = state.get("raw_batch_path")
    if not raw_batch_path:
        return []
    return storage.load_raw_pages_from_batch(raw_batch_path)


def _endpoint_raw_batch_path(storage: StorageManager, username: str, endpoint: str) -> Optional[Path]:
    state = storage.get_endpoint_state(username, endpoint)
    raw_batch_path = state.get("raw_batch_path")
    if not raw_batch_path:
        return None
    path = Path(str(raw_batch_path))
    return path if path.exists() and path.is_dir() else None


def _max_pages_for(engine: FetcherEngine, username: str) -> int:
    policy = get_priority_policy(username, engine.account_map, engine.priority_policies)
    return int(policy.get("historical_max_pages", DEFAULT_HISTORICAL_MAX_PAGES))


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
        print(f"[V4] @{username} {endpoint} raw verified: {len(page_files)} page file(s)")
    else:
        print(
            f"[V4] @{username} {endpoint} raw verification warning: "
            f"loaded_pages={len(raw_pages)} page_files={len(page_files)}"
        )
    return ok


def _verify_txt_files(username: str, label: str, paths: List[Path]) -> bool:
    missing_or_empty = [path for path in paths if not path.exists() or path.stat().st_size == 0]
    if not paths or missing_or_empty:
        print(
            f"[V4] @{username} {label} TXT verification warning: "
            f"files={len(paths)} missing_or_empty={len(missing_or_empty)}"
        )
        return False
    print(f"[V4] @{username} {label} TXT verified: {len(paths)} file(s)")
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
    list_replies_only = processor.get_difference_b_minus_a(set_a, set_b)

    txt_outputs = {
        "1_user_tweets": storage.save_processed_txt_set(list_a, "1_user_tweets", username),
        "2_user_tweets_and_replies": storage.save_processed_txt_set(list_b, "2_user_tweets_and_replies", username),
        "3_intersection": storage.save_processed_txt_set(list_intersection, "3_intersection", username),
        "4_union": storage.save_processed_txt_set(list_union, "4_union", username),
        "5_replies_only": storage.save_processed_txt_set(list_replies_only, "5_replies_only", username),
    }
    verified = all(_verify_txt_files(username, set_name, paths) for set_name, paths in txt_outputs.items())

    print(
        f"[V4] @{username} processed | "
        f"tweets={len(list_a)} replies_endpoint={len(list_b)} "
        f"intersection={len(list_intersection)} union={len(list_union)} "
        f"replies_only={len(list_replies_only)}"
    )
    return verified, {
        "tweets": len(list_a),
        "replies_endpoint": len(list_b),
        "intersection": len(list_intersection),
        "union": len(list_union),
        "replies_only": len(list_replies_only),
    }


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
    paths = storage.save_processed_txt_set(list(extracted.values()), set_name, username)
    print(f"[V4] @{username} {endpoint} TXT updated: {len(extracted)} item(s)")
    raw_ok = _verify_raw_pages(
        storage=storage,
        username=username,
        endpoint=endpoint,
        raw_pages=raw_pages,
    )
    txt_ok = _verify_txt_files(username, set_name, paths)
    return raw_ok and txt_ok


def _safe_endpoint_report(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": result.get("status"),
        "outcome": result.get("outcome"),
        "reason": result.get("reason"),
        "pages_fetched": result.get("pages_fetched", 0),
        "raw_batch_path": result.get("raw_batch_path"),
        "last_cursor": result.get("last_cursor"),
        "last_http_status": result.get("last_http_status"),
        "attempts": result.get("attempts", 0),
        "error_samples": result.get("error_samples", []),
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
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
    print("[V4] Run summary:")
    for key, value in report.get("summary", {}).items():
        print(f"[V4]   {key}: {value}")

    grouped: Dict[str, List[str]] = {"failed": [], "partial": [], "skipped": []}
    for username, account_report in report.get("accounts", {}).items():
        for endpoint, endpoint_report in (account_report.get("endpoints", {}) or {}).items():
            status = endpoint_report.get("status")
            if status in grouped:
                grouped[status].append(f"@{username} {endpoint} ({endpoint_report.get('outcome')})")
    for status, items in grouped.items():
        if items:
            print(f"[V4]   {status}: {', '.join(items)}")
    print(f"[V4] Report JSON: {json_path}")
    print(f"[V4] Report TXT: {txt_path}")


def run_v4(selected_accounts: Optional[List[str]] = None) -> None:
    project_root = Path(__file__).resolve().parent
    engine = FetcherEngine(config_path="config/config.json")
    storage = StorageManager(project_root=project_root)
    processor = TweetSetProcessor()

    accounts = selected_accounts or ordered_accounts(engine.account_map)
    if not accounts:
        print("[V4] No accounts found in tier configuration.")
        return

    accounts = [account.strip().lstrip("@") for account in accounts if account and account.strip()]
    run_id = storage.create_run_id()
    report: Dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.utcnow().isoformat() + "Z",
        "config": {
            "endpoint_order": ["UserTweetsAndReplies", "UserTweets"],
            "accounts_requested": len(accounts),
        },
        "summary": {},
        "accounts": {},
    }
    user_ids: Dict[str, str] = {}
    active_accounts: List[str] = []
    fetched_pages: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        username: {} for username in accounts
    }

    print(f"[V4] Phase 1/4: resolving user ids for {len(accounts)} account(s)")
    for username in accounts:
        storage.ensure_account_state(username)
        report["accounts"].setdefault(username, {"endpoints": {}})
        try:
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
            print(f"[V4] @{username} skipped for this run: {reason}")
            continue
        active_accounts.append(username)
        report["accounts"][username]["user_id"] = user_ids[username]
        print(f"[V4] @{username} -> user_id={user_ids[username]}")

    if not active_accounts:
        print("[V4] No accounts with resolved user ids; nothing to fetch.")
        report["finished_at"] = datetime.utcnow().isoformat() + "Z"
        _update_report_summary(report)
        json_path = storage.save_run_report_json(report, run_id)
        txt_path = storage.save_run_report_txt(report, run_id)
        _print_report_summary(report, json_path, txt_path)
        return

    print("[V4] Phase 2/4: fetching UserTweetsAndReplies for all accounts")
    for idx, username in enumerate(active_accounts):
        max_pages = _max_pages_for(engine, username)
        result = engine._fetch_endpoint_result(
            account=username,
            user_id=user_ids[username],
            endpoint="UserTweetsAndReplies",
            max_pages=max_pages,
        )
        fetched_pages[username]["UserTweetsAndReplies"] = result.get("pages", [])
        endpoint_verified = _save_endpoint_processed_txt(
            storage=storage,
            processor=processor,
            username=username,
            endpoint="UserTweetsAndReplies",
            raw_pages=fetched_pages[username]["UserTweetsAndReplies"],
        )
        endpoint_report = _safe_endpoint_report(result)
        endpoint_report["processed_txt_verified"] = endpoint_verified
        report["accounts"][username]["endpoints"]["UserTweetsAndReplies"] = endpoint_report
        storage.update_endpoint_state(
            username,
            "UserTweetsAndReplies",
            meta={
                "processed_txt_verified": endpoint_verified,
                "run_id": run_id,
                "outcome": result.get("outcome"),
                "pages_fetched": result.get("pages_fetched", 0),
            },
        )
        if idx < len(active_accounts) - 1:
            engine.api_manager.human_delay("between_accounts")

    print("[V4] Phase 3/4: fetching UserTweets for all accounts")
    for idx, username in enumerate(active_accounts):
        max_pages = _max_pages_for(engine, username)
        result = engine._fetch_endpoint_result(
            account=username,
            user_id=user_ids[username],
            endpoint="UserTweets",
            max_pages=max_pages,
        )
        fetched_pages[username]["UserTweets"] = result.get("pages", [])
        endpoint_verified = _save_endpoint_processed_txt(
            storage=storage,
            processor=processor,
            username=username,
            endpoint="UserTweets",
            raw_pages=fetched_pages[username]["UserTweets"],
        )
        endpoint_report = _safe_endpoint_report(result)
        endpoint_report["processed_txt_verified"] = endpoint_verified
        report["accounts"][username]["endpoints"]["UserTweets"] = endpoint_report
        storage.update_endpoint_state(
            username,
            "UserTweets",
            meta={
                "processed_txt_verified": endpoint_verified,
                "run_id": run_id,
                "outcome": result.get("outcome"),
                "pages_fetched": result.get("pages_fetched", 0),
            },
        )
        if idx < len(active_accounts) - 1:
            engine.api_manager.human_delay("between_accounts")

    print("[V4] Phase 4/4: generating processed TXT sets")
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
    print("[V4] Replies-first pipeline complete.")


if __name__ == "__main__":
    run_v4()
