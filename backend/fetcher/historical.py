#!/usr/bin/env python3
"""Historical profile-timeline backfill runner.

Fetches UserTweets (and optionally UserTweetsAndReplies) for one or more
accounts, then builds the seven processed set-algebra folders per account under
``data/historical_live/`` and writes a per-run report.

Run:
    python -m fetcher.historical --only elonmusk
    python -m fetcher.historical --only elonmusk

Flags:
    --only <user>              account to fetch (repeatable / comma-separated)
    --only-account <user>      alias for --only
    --no-user-tweets           skip UserTweets (on by default)
    --no-with-replies          skip UserTweetsAndReplies (on by default)
"""
from __future__ import annotations


import argparse
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fetcher.config import PROJECT_ROOT
from fetcher.clock import utc_now_iso

from fetcher.config import get_priority_policy, ordered_accounts
from fetcher.timeline import FetcherEngine
from fetcher.processing import TZ, RollingWindowEvaluator, TweetSetProcessor
from fetcher.storage import StorageManager
from fetcher.observability import attach_run_id
from fetcher.observability import PipelineConsole


ENDPOINTS = ("UserTweets",)
CONSOLE = PipelineConsole("historical")

# Pages this account may fetch in one tick. The archive walk is resumable, so a
# tick is a bite of a long job rather than an attempt to finish it: small enough
# to land well inside the Celery wall-clock timeout, frequent enough that the
# whole fleet keeps moving. Set from Django settings by fetching.runner.
PAGES_PER_TICK = max(1, int(os.environ.get("TDF_HISTORICAL_PAGES_PER_TICK", "25")))

# Requests the archive walk must leave in the shared UserTweets bucket for the
# live poller, which reserves 5 more on top of this for itself. Without a floor
# the deep walk drained the bucket every tick and live deferred every account.
QUOTA_FLOOR = max(0, int(os.environ.get("TDF_HISTORICAL_QUOTA_FLOOR", "20")))


DEFAULT_ARCHIVE_EARLIEST_DATE = "2024-01-01"


def _archive_cutoff() -> Optional[datetime]:
    """The date the archive walk deliberately stops at, or None for no floor.

    Tehran-aware, because that is what it gets compared against: tweet times
    come back from processing.tweet_datetime already in TZ, and Python refuses
    to order an aware datetime against a naive one. A naive value here does not
    degrade -- it raises TypeError inside the per-page coverage check, outside
    any handler, so the walk dies on page 1 of every account before
    _record_backfill_progress can store a cursor, and the next tick restarts
    from the top and burns the shared request budget again.

    A malformed value falls back to the shipped default rather than to None: no
    floor at all is the one outcome this setting exists to prevent, and a
    one-character typo in .env should not silently uncap the archive.
    """
    raw = (os.environ.get("TDF_ARCHIVE_EARLIEST_DATE") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        CONSOLE.warning(
            f"TDF_ARCHIVE_EARLIEST_DATE={raw!r} is not YYYY-MM-DD; "
            f"falling back to {DEFAULT_ARCHIVE_EARLIEST_DATE}"
        )
        parsed = datetime.strptime(DEFAULT_ARCHIVE_EARLIEST_DATE, "%Y-%m-%d")
    return parsed.replace(tzinfo=TZ)


# Why a walk stopped, kept apart from *whether* it stopped. Only the first of
# these means "this account's history is collected"; the second means X refused
# to page any deeper, which is a fact about the provider, not about the account.
DEPTH_REACHED_FLOOR = "reached_date_floor"
DEPTH_PROVIDER_LIMIT = "provider_depth_limit"
DEPTH_TRUE_END = "reached_first_tweet"


# Raw page loading and verification -----------------------------------------


def _endpoint_pages(storage: StorageManager, username: str, endpoint: str) -> List[Dict[str, Any]]:
    state = storage.get_endpoint_state(username, endpoint)
    raw_batch_path = state.get("raw_batch_path")
    if not raw_batch_path:
        return storage.load_all_raw_pages(endpoint, username)
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
    raw_pages = storage.load_all_raw_pages(endpoint, username)
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


def _verify_processed(username: str, label: str, path: Path) -> bool:
    """Confirm the processed set actually landed on disk and is non-empty."""
    if not path.exists() or path.stat().st_size == 0:
        CONSOLE.warning(f"@{username} {label} not written or empty: {path.name}")
        return False
    CONSOLE.success(f"@{username} {label} verified")
    return True


def _process_account(
    *,
    storage: StorageManager,
    processor: TweetSetProcessor,
    username: str,
    raw_pages_by_endpoint: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Tuple[bool, Dict[str, int]]:
    raw_pages_by_endpoint = raw_pages_by_endpoint or {}
    raw_tweets = raw_pages_by_endpoint.get("UserTweets") or _endpoint_pages(
        storage, username, "UserTweets"
    )
    set_a = processor.extract_tweets_from_raw(
        raw_tweets,
        username=username,
        source_endpoint="UserTweets",
    )
    list_union = list(set_a.values())
    path = storage.save_processed_set_merged(list_union, "4_union", username)
    verified = _verify_processed(username, "4_union", path)
    CONSOLE.success(f"@{username} processed | union={len(list_union)}")
    return verified, {"union": len(list_union), "sets_written": ["4_union"]}


# Processed output and reporting --------------------------------------------


def _save_endpoint_processed(
    *,
    storage: StorageManager,
    processor: TweetSetProcessor,
    username: str,
    endpoint: str,
    raw_pages: List[Dict[str, Any]],
) -> bool:
    if not raw_pages:
        raw_pages = _endpoint_pages(storage, username, endpoint)
    set_name = "4_union"
    extracted = processor.extract_tweets_from_raw(
        raw_pages,
        username=username,
        source_endpoint=endpoint,
    )
    path = storage.save_processed_set_merged(list(extracted.values()), set_name, username)
    CONSOLE.info(f"@{username} {endpoint} processed: {len(extracted)} item(s)")
    raw_ok = _verify_raw_pages(
        storage=storage,
        username=username,
        endpoint=endpoint,
        raw_pages=raw_pages,
    )
    processed_ok = _verify_processed(username, set_name, path)
    return raw_ok and processed_ok


def _safe_endpoint_report(result: Dict[str, Any]) -> Dict[str, Any]:
    status = result.get("status")
    transport = result.get("transport")
    # Browser fallback on profile timelines is a degraded path — never "successful".
    if transport == "browser_fallback" and status == "completed":
        status = "partial"
        result = {**result, "status": "partial", "outcome": result.get("outcome") or "partial_browser_fallback"}
    endpoint_status = (
        "partial_browser_fallback"
        if transport == "browser_fallback"
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
            if endpoint_report.get("processed_verified") is False:
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

    state = storage.get_endpoint_state(username, endpoint)
    backfill_cursor = state.get("backfill_cursor")
    if backfill_cursor:
        CONSOLE.info(f"@{username} {endpoint} resuming archive walk at page {int(state.get('backfill_pages_done', 0)) + 1}")
    elif state.get("backfill_complete"):
        # A previously-finished account is being walked again from the top --
        # only fetch_account_historical can get here, since the scheduler skips
        # complete accounts. Retire the old verdict now, at the one moment a new
        # walk actually begins, so a completion flag can never sit beside a live
        # cursor into unfetched history. Doing this per tick instead would let a
        # single failed manual refetch un-complete a genuinely finished archive
        # and drop it back into the queue for a full re-walk.
        CONSOLE.info(f"@{username} {endpoint} re-walking a completed archive from the top")
        state = {**state, "backfill_complete": False, "backfill_depth_reason": None}
        storage.update_endpoint_state(username, endpoint, meta={
            "backfill_complete": False,
            "backfill_depth_reason": None,
            "backfill_completed_at": None,
            "backfill_pages_done": 0,
        })

    # The only cutoff here is the absolute archive floor -- never the tier's
    # rolling 2-7 day window, which would stop the walk a few pages in and is
    # how a previous revision marked an active account fully archived after
    # covering one week of it. Keeping the recent window fresh is live polling's
    # job. Depth is bounded by the page budget, the quota floor, this floor, and
    # the end of the timeline -- nothing else.
    cutoff = _archive_cutoff()
    result = engine._fetch_endpoint_result(
        account=username,
        user_id=user_id,
        endpoint=endpoint,
        max_pages=PAGES_PER_TICK,
        window_days=None,
        cutoff=cutoff,
        force_refetch=not backfill_cursor,
        min_remaining=QUOTA_FLOOR,
        resume_cursor=backfill_cursor,
    )
    _record_backfill_progress(storage, username, endpoint, state, result, cutoff=cutoff)
    return result, window_coverage


def _record_backfill_progress(
    storage: StorageManager,
    username: str,
    endpoint: str,
    previous: Dict[str, Any],
    result: Dict[str, Any],
    cutoff: Optional[datetime] = None,
) -> None:
    """Save where this tick's archive walk stopped so the next one resumes there.

    ``last_cursor`` is not usable for this: the shallow live poll writes to the
    same (account, endpoint) row and the two walks sit at different depths. The
    archive keeps its own cursor, and only a run that proved it reached the end
    of the timeline is allowed to mark the walk complete.
    """
    fetched = int(result.get("pages_fetched", 0) or 0)
    outcome = str(result.get("outcome") or "")
    pages_done = int(previous.get("backfill_pages_done", 0) or 0) + fetched
    # Completion is decided by the outcome, never by the status. A run that
    # merely satisfied a rolling window also reports status="completed" while
    # still holding a live cursor, and treating that as the end is how an active
    # account got marked permanently archived after a few pages.
    #
    # Three different things end a walk and they are NOT interchangeable:
    #
    #  - success_window_complete: crossed the archive date floor. The walk only
    #    ever passes that floor as its cutoff, so here this is a real, intended
    #    completion.
    #  - success_true_end: X ran out of cursor. The account's first tweet.
    #  - success_timeline_exhausted: X kept offering a cursor but stopped
    #    returning tweets. This is X's serving depth, not the account's first
    #    tweet -- it fires after two empty pages, and treating it as "we have
    #    everything" is what put 45 of 64 accounts at exactly 45 pages and let
    #    @elonmusk be reported fully archived with three months of history.
    #    It still ends the walk (retrying cannot get past a provider limit) but
    #    it is recorded as a provider limit, not as completeness.
    depth_reason = {
        "success_window_complete": DEPTH_REACHED_FLOOR,
        "success_true_end": DEPTH_TRUE_END,
        "success_timeline_exhausted": DEPTH_PROVIDER_LIMIT,
    }.get(outcome)
    finished = depth_reason is not None
    cursor = result.get("last_cursor")
    # A tick that fetched nothing made no progress. The queue sorts on this so an
    # account that cannot advance (dead session, permanent 404) sinks to the back
    # instead of blocking every account behind it -- which is exactly how one
    # stuck account starved the whole fleet before. Pausing at the shared quota
    # floor is explicitly not a stall: it is the system working as designed, and
    # counting it would demote whichever healthy accounts happen to sit late in a
    # chunk that ran the bucket down.
    stalled = int(previous.get("backfill_stalled_ticks", 0) or 0)
    meta: Dict[str, Any] = {
        "backfill_pages_done": pages_done,
        "backfill_last_outcome": outcome,
        "backfill_stalled_ticks": (
            stalled if outcome == "paused_for_quota" else (0 if fetched else stalled + 1)
        ),
        # Which floor this verdict was reached against. Without it, lowering
        # FETCH_ARCHIVE_EARLIEST_DATE silently invalidates every account marked
        # reached_date_floor with no way to find them again.
        "backfill_floor_date": (cutoff.strftime("%Y-%m-%d") if cutoff else None),
    }
    # Only ever *set* completion here, never clear it: this function sees one
    # tick, and a non-terminal tick carries no evidence that the account is
    # incomplete. Clearing belongs at the one place a new walk starts (see
    # _fetch_or_skip_endpoint), which is what keeps the flag from contradicting
    # the cursor beside it without un-completing finished archives.
    if finished:
        meta["backfill_complete"] = True
        meta["backfill_depth_reason"] = depth_reason
        meta["backfill_cursor"] = None
        meta["backfill_completed_at"] = utc_now_iso()
        if depth_reason == DEPTH_PROVIDER_LIMIT:
            CONSOLE.warning(
                f"@{username} {endpoint} stopped at X's serving depth after "
                f"{pages_done} page(s) -- this is as deep as the API goes, "
                f"not the account's first tweet"
            )
        else:
            CONSOLE.success(
                f"@{username} {endpoint} archive walk complete after "
                f"{pages_done} page(s) ({depth_reason})"
            )
    elif cursor and str(cursor) not in {"__START__", "__END__"}:
        meta["backfill_cursor"] = str(cursor)
    # Any other outcome (a hard failure before the first page) leaves the stored
    # cursor untouched, so the next tick retries the same position rather than
    # silently restarting the account from the top.
    storage.update_endpoint_state(username, endpoint, meta=meta)


def run_v4(
    selected_accounts: Optional[List[str]] = None,
) -> None:
    project_root = PROJECT_ROOT
    engine = FetcherEngine()
    storage = StorageManager(
        project_root=project_root,
        subsystem="historical_live",
        data_root_override=engine.data_root,
    )
    processor = TweetSetProcessor()
    evaluator = RollingWindowEvaluator()
    console = engine.logger

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
    engine.recorder.emit("run_start", accounts=accounts)
    report: Dict[str, Any] = {
        "run_id": run_id,
        "started_at": utc_now_iso(),
        "config": {
            "endpoint_order": ["UserTweets"],
            "enabled_endpoints": ["UserTweets"],
            "accounts_requested": len(accounts),
            "completion_rule": "tehran_rolling_window",
            "pagination_safety_cap_pages": engine.pagination_safety_cap_pages,
            "first_request_warmup_seconds": engine.first_request_warmup_seconds,
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

    console.phase_banner("Resolve user IDs", pass_index=1, pass_total=3, account_count=len(accounts))
    engine.recorder.emit_phase_start(phase="resolve_user_ids", accounts=len(accounts), pass_index=1, pass_total=3)
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
                    "processed_verified": None,
                }
            console.error(f"@{username} skipped for this run: {reason}")
            continue
        active_accounts.append(username)
        report["accounts"][username]["user_id"] = user_ids[username]
        console.success(f"@{username} -> user_id={user_ids[username]}")

    if not active_accounts:
        console.warning("No accounts with resolved user IDs; nothing to fetch.")
        report["finished_at"] = utc_now_iso()
        _update_report_summary(report)
        json_path = storage.save_run_report_json(report, run_id)
        txt_path = storage.save_run_report_txt(report, run_id)
        _print_report_summary(report, json_path, txt_path)
        engine.recorder.emit("run_end", summary=report["summary"], report_json=str(json_path))
        return

    enabled_endpoints = list(ENDPOINTS)

    console.phase_banner("Global endpoint fetching", pass_index=2, pass_total=3, account_count=len(active_accounts))
    for endpoint_index, endpoint in enumerate(enabled_endpoints, start=1):
        engine.recorder.emit_phase_start(
            phase="fetch_endpoint",
            endpoint=endpoint,
            accounts=len(active_accounts),
            pass_index=endpoint_index,
            pass_total=len(enabled_endpoints),
        )
        for idx, username in enumerate(active_accounts):
            if idx > 0:
                engine.api_manager.human_delay("between_accounts")
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
            endpoint_verified = _save_endpoint_processed(
                storage=storage,
                processor=processor,
                username=username,
                endpoint=endpoint,
                raw_pages=fetched_pages[username][endpoint],
            )
            endpoint_report = _safe_endpoint_report(result)
            endpoint_report["processed_verified"] = endpoint_verified
            report["accounts"][username]["endpoints"][endpoint] = endpoint_report
            storage.update_endpoint_state(
                username,
                endpoint,
                meta={
                    "processed_verified": endpoint_verified,
                    "run_id": run_id,
                    "outcome": result.get("outcome"),
                    "pages_fetched": result.get("pages_fetched", 0),
                    "window_coverage": result.get("window_coverage") or window_coverage,
                },
            )

    console.phase_banner("Generate processed sets", pass_index=3, pass_total=3, account_count=len(active_accounts))
    engine.recorder.emit_phase_start(phase="generate_processed_sets", accounts=len(active_accounts), pass_index=3, pass_total=3)
    storage_cfg = engine.api_manager.config.get("storage") or {}
    raw_keep = int(storage_cfg.get("raw_batch_retention_count", 0) or 0)
    for username in active_accounts:
        final_verified, counts = _process_account(
            storage=storage,
            processor=processor,
            username=username,
            raw_pages_by_endpoint=fetched_pages.get(username),
        )
        if raw_keep > 0:
            for endpoint in ENDPOINTS:
                storage.prune_raw_batches(endpoint, username, keep=raw_keep)
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

    report["finished_at"] = utc_now_iso()
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
    )


if __name__ == "__main__":
    main()
