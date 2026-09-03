"""Run the canonical fetcher in an isolated subprocess and ingest results.

Each run gets an ephemeral scratch PROJECT_ROOT (TDF_PROJECT_ROOT) so the
engine's on-disk layer never persists. Durable continuity (sync-state
watermarks/cursors, tx/query health) is round-tripped through Postgres
KeyValueState before and after the run. Postgres is the sole durable store.
"""
from __future__ import annotations

import json
import logging
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Iterable

from django.conf import settings
from django.utils import timezone

from tweets.models import EndpointState, FetchRun, KeyValueState, RawPage, XSession

from .redaction import _literal_secrets, redact_text

from .accounts import tracked_accounts_payload

logger = logging.getLogger(__name__)

SEED_DIR = Path(settings.BASE_DIR) / "seed"

# Mirrors SearchQueryBuilder.slug so the runner can locate processed search exports.
_SLUG_KEEP = re.compile(r"[^A-Za-z0-9_\\-]+")


@dataclass(frozen=True)
class FetcherRunResult:
    root: Path
    run: FetchRun


def normalize_slug(raw: str) -> str:
    """Normalize a search slug the same way the vendored SearchQueryBuilder does.

    The engine writes processed exports under ``data/search/processed/<slug>/``
    where ``<slug>`` is the normalized form (lowercased, non-alnum runs → ``_``).
    The runner must use the same form to locate those files.
    """
    return _SLUG_KEEP.sub("_", str(raw)).strip("_").lower() or "search_timeline"


def _active_session() -> XSession | None:
    return XSession.objects.filter(active=True).first()


def _search_def(search) -> dict:
    """Minimal searches.json entry the canonical search pipeline understands."""
    return {
        "name": search.name,
        "slug": search.slug,
        "enabled": True,
        "product": search.product,
        "preserve_exact_query": True,
        "raw_query": search.raw_query,
        "pagination_depth": max(1, int(search.pagination_depth)),
        "max_retries": 3,
        # The rolling window is a hard pagination stop: runs that did reach page
        # 2-3 ended on `success_search_window_crossed`, not on running out of
        # results. Hardcoding 24h capped a search whose query spans months.
        "rolling_hours": max(1, int(search.rolling_hours)),
    }


def _write_config(root: Path, searches: list | None = None) -> Path:
    """Materialize config/config.json from the shared XSession + seed files."""
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "accounts.json").write_text(
        json.dumps(tracked_accounts_payload(), indent=2), encoding="utf-8"
    )
    # searches.json comes from the DB when provided, else the seed file.
    if searches is not None:
        (config_dir / "searches.json").write_text(
            json.dumps([_search_def(s) for s in searches], indent=2), encoding="utf-8"
        )
    elif (SEED_DIR / "searches.json").exists():
        shutil.copy2(SEED_DIR / "searches.json", config_dir / "searches.json")

    base = {}
    example = SEED_DIR / "config.example.json"
    if example.exists():
        base = json.loads(example.read_text(encoding="utf-8"))
    session = _active_session()
    if session:
        # Session-bound config (captured tx-id pools, query-id pools) first, so
        # the seed template supplies only the values the session does not carry.
        if isinstance(session.config_overrides, dict):
            for key, value in session.config_overrides.items():
                if isinstance(value, dict) and isinstance(base.get(key), dict):
                    base[key] = {**base[key], **value}
                else:
                    base[key] = value
        headers = {str(key): str(value) for key, value in session.headers.items() if value}
        base["api_cookies"] = {str(key): str(value) for key, value in session.cookies.items() if value}
        base["api_headers"] = headers
        authorization = next((value for key, value in headers.items() if key.lower() == "authorization"), "")
        if authorization.lower().startswith("bearer "):
            base.setdefault("api_auth", {})["bearer_token"] = authorization[7:].strip()
        # Snapshot what this run started with so _persist_session can tell
        # whether *this* subprocess actually refreshed the session, instead of
        # blindly resaving on every run (see _persist_session).
        (config_dir / "_session_snapshot.json").write_text(
            json.dumps({"api_cookies": base["api_cookies"], "api_headers": base["api_headers"]}),
            encoding="utf-8",
        )
    (config_dir / "config.json").write_text(json.dumps(base, indent=2), encoding="utf-8")
    return config_dir / "config.json"


def _restore_state(root: Path, subsystem: str) -> None:
    """Seed the scratch state dir from Postgres so watermarks/cursors persist."""
    sub = "historical_live" if subsystem in ("historical", "live") else subsystem
    state_dir = root / "data" / sub / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sync = KeyValueState.objects.filter(namespace="sync_state", name=sub).first()
    if sync:
        (state_dir / "sync_state.json").write_text(json.dumps(sync.data), encoding="utf-8")
    prefix = f"{sub}:"
    for row in KeyValueState.objects.filter(namespace="request_state"):
        if row.name.startswith(prefix):
            filename = row.name[len(prefix):]
        elif ":" not in row.name and sub == "historical_live":
            filename = row.name
        else:
            continue
        (state_dir / filename).write_text(json.dumps(row.data), encoding="utf-8")


def _request_state_name(subsystem: str, filename: str) -> str:
    sub = "historical_live" if subsystem in ("historical", "live") else subsystem
    return f"{sub}:{filename}"


def _persist_state(root: Path, subsystem: str) -> None:
    sub = "historical_live" if subsystem in ("historical", "live") else subsystem
    state_dir = root / "data" / sub / "state"
    sync_file = state_dir / "sync_state.json"
    if sync_file.exists():
        data = _read_json(sync_file)
        if isinstance(data, dict):
            KeyValueState.objects.update_or_create(
                namespace="sync_state", name=sub, defaults={"data": data},
            )
    for f in state_dir.glob("*.json"):
        if f.name == "sync_state.json":
            continue
        data = _read_json(f)
        if not isinstance(data, dict):
            continue
        KeyValueState.objects.update_or_create(
            namespace="request_state",
            name=_request_state_name(subsystem, f.name),
            defaults={"data": data},
        )


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _persist_endpoint_states(root: Path, subsystem: str) -> int:
    sub = "historical_live" if subsystem in ("historical", "live") else subsystem
    state_dir = root / "data" / sub / "state"
    count = 0
    sync = _read_json(state_dir / "sync_state.json", {})
    if isinstance(sync, dict):
        for account, account_state in sync.items():
            if not isinstance(account_state, dict):
                continue
            for endpoint in ("UserTweets", "UserTweetsAndReplies"):
                data = account_state.get(endpoint)
                if isinstance(data, dict):
                    EndpointState.objects.update_or_create(
                        account=str(account), endpoint=endpoint, defaults={"data": data}
                    )
                    count += 1
    search_state = _read_json(state_dir / "search_state.json", {})
    if isinstance(search_state, dict):
        for target, data in search_state.items():
            if isinstance(data, dict):
                EndpointState.objects.update_or_create(
                    account=str(target), endpoint="SearchTimeline", defaults={"data": data}
                )
                count += 1
    return count


# Raw pages are JSONB blobs measured in hundreds of kilobytes, so they are
# flushed in small groups: one bulk upsert per page was a round trip each, and
# holding every page of a run in memory before a single write is how a worker
# with a 512m ceiling gets OOM-killed mid-cycle.
_RAW_PAGE_WRITE_CHUNK = 100


def _flush_raw_pages(rows: list[RawPage]) -> int:
    if not rows:
        return 0
    RawPage.objects.bulk_create(
        rows,
        update_conflicts=True,
        unique_fields=["endpoint", "account", "batch", "page_number"],
        update_fields=["payload", "fetch_run"],
    )
    return len(rows)


def _persist_raw_pages(root: Path, subsystem: str, run: FetchRun) -> int:
    sub = "historical_live" if subsystem in ("historical", "live") else subsystem
    raw_root = root / "data" / sub / "raw"
    count = 0
    pending: list[RawPage] = []
    # Postgres refuses an ON CONFLICT DO UPDATE that touches one row twice in a
    # single statement, and the key is derived from the path (`page_1.json` and
    # `page_01.json` both read as page 1), so collisions are deduplicated before
    # they can reach the batch rather than aborting the whole write.
    seen: set[tuple[str, str, str, int]] = set()
    for path in raw_root.rglob("page_*.json") if raw_root.exists() else ():
        payload = _read_json(path)
        if not isinstance(payload, dict):
            continue
        relative = path.relative_to(raw_root).parts
        if sub == "historical_live" and len(relative) >= 4:
            endpoint, account, batch = relative[0], relative[1], relative[2]
        elif sub == "search" and len(relative) >= 4:
            endpoint, account, batch = "SearchTimeline", f"{relative[0]}:{relative[1]}", relative[2]
        else:
            continue
        match = re.search(r"(\d+)$", path.stem)
        if not match:
            continue
        key = (endpoint, account, batch, int(match.group(1)))
        if key in seen:
            continue
        seen.add(key)
        pending.append(
            RawPage(
                endpoint=endpoint,
                account=account,
                batch=batch,
                page_number=key[3],
                payload=payload,
                fetch_run=run,
            )
        )
        if len(pending) >= _RAW_PAGE_WRITE_CHUNK:
            count += _flush_raw_pages(pending)
            pending = []
    count += _flush_raw_pages(pending)
    # No purge here any more. This used to run an uncapped delete loop at a
    # hardcoded 7 days, inside every fetch run, on the fetch worker -- which both
    # contradicted RAW_PAGE_RETENTION_DAYS (the setting was dead) and spent the
    # run's wall-clock budget on maintenance. Retention is now solely
    # fetching.tasks.purge_old_raw_pages: chunked, row-capped, on the control
    # queue, and governed by that one setting.
    return count


def _collect_run_summary(root: Path, subsystem: str, return_code: int) -> tuple[dict, dict, str]:
    sub = "historical_live" if subsystem in ("historical", "live") else subsystem
    base = root / "data" / sub
    event_counts: Counter[str] = Counter()
    # Which endpoint the run's requests actually went to. event_counts gives the
    # total, and recent_events is capped at 100, so neither can attribute a long
    # run's spend to UserTweets vs SearchTimeline -- this can.
    pages_by_endpoint: Counter[str] = Counter()
    items_by_endpoint: Counter[str] = Counter()
    recent_events = []
    for event_file in (base / "logs").rglob("events.jsonl") if (base / "logs").exists() else ():
        for line in event_file.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            event_type = str(event.get("type") or "unknown")
            event_counts[event_type] += 1
            if event_type == "page_fetched":
                endpoint = str(event.get("endpoint") or "unknown")
                pages_by_endpoint[endpoint] += 1
                try:
                    items_by_endpoint[endpoint] += int(event.get("items") or 0)
                except (TypeError, ValueError):
                    pass
            recent_events.append(event)
    recent_events = recent_events[-100:]

    http_summary = _read_json(base / "logs" / "http_summary.json", {})
    if not isinstance(http_summary, dict):
        http_summary = {}
    failure_ledger = http_summary.get("failure_ledger", {})
    report_summaries = []
    status_counts: Counter[str] = Counter()
    for report_file in (base / "reports").glob("*.json") if (base / "reports").exists() else ():
        report = _read_json(report_file, {})
        if not isinstance(report, dict):
            continue
        if isinstance(report.get("summary"), dict):
            report_summary = report["summary"]
            status_counts["partial"] += int(report_summary.get("partial_endpoints", 0) or 0)
            status_counts["failed"] += int(report_summary.get("failed_endpoints", 0) or 0)
            status_counts["completed"] += int(report_summary.get("successful_endpoints", 0) or 0)
        else:
            report_summary = {
                "status": report.get("status"),
                "endpoint_status": report.get("endpoint_status"),
                "counts": report.get("counts", {}),
                "exhausted_reason": (report.get("metadata") or {}).get("exhausted_reason"),
            }
            status_counts[str(report.get("status") or "unknown")] += 1
        report_summaries.append({"file": report_file.name, **report_summary})

    by_status = http_summary.get("by_status_code", {})
    no_evidence = not report_summaries
    if int(by_status.get("401", 0) or 0) or int(by_status.get("403", 0) or 0):
        status = "auth_required"
    elif return_code != 0 or status_counts["failed"]:
        status = "failed"
    elif status_counts["partial"]:
        status = "partial"
    elif no_evidence:
        # Exit 0 but the pipeline wrote no per-target report, so nothing is known
        # to have been fetched. Reporting "completed" here is how a run that did
        # nothing used to look identical to a healthy one.
        status = "partial"
    else:
        status = "completed"
    return {
        "return_code": return_code,
        "event_counts": dict(event_counts),
        "pages_by_endpoint": dict(pages_by_endpoint),
        "items_by_endpoint": dict(items_by_endpoint),
        # http_summary counts errors only (EventRecorder._increment_summary is
        # called from emit_http_error), so these are the failed requests per
        # endpoint/status -- pages_by_endpoint above is the successful ones.
        "http_errors_by_endpoint": http_summary.get("by_endpoint", {}),
        "http_errors_by_status": by_status,
        "recent_events": recent_events,
        "reports": report_summaries,
        "status_counts": dict(status_counts),
        **({"status_reason": "no_reports_written"} if no_evidence else {}),
    }, failure_ledger, status


# Statuses whose raw pages are worth keeping, once the measurement below has
# told us what each one actually costs. Empty means "keep everything", which is
# today's behaviour and the baseline the measurement is taken against.
#
# The candidate rule is {"failed", "auth_required"} -- the two states where the
# bytes are the only way to see what X actually returned. "partial" is the open
# question: this codebase deliberately records a run that did nothing as
# partial, so it may be common enough that gating on it saves little.
RAW_PAGE_KEEP_STATUSES: frozenset[str] = frozenset()


def _count_raw_pages(root: Path, subsystem: str) -> int:
    """How many pages this run *would* write, without writing them."""
    sub = "historical_live" if subsystem in ("historical", "live") else subsystem
    raw_root = root / "data" / sub / "raw"
    if not raw_root.exists():
        return 0
    return sum(1 for _ in raw_root.rglob("page_*.json"))


def _persist_artifacts(root: Path, subsystem: str, run: FetchRun, return_code: int) -> tuple[dict, dict, str]:
    summary, failure_ledger, status = _collect_run_summary(root, subsystem, return_code)
    keep = not RAW_PAGE_KEEP_STATUSES or status in RAW_PAGE_KEEP_STATUSES
    pages = _persist_raw_pages(root, subsystem, run) if keep else 0
    summary["raw_pages"] = pages
    # The census, not the gate. Raw pages are ~91% of the database and nothing
    # reads them, so they should only survive for runs worth debugging -- but
    # which statuses those are is an empirical question. This line is what makes
    # it answerable; grep a week of it, then set RAW_PAGE_KEEP_STATUSES above.
    logger.info(
        "raw_page_census subsystem=%s status=%s pages=%d kept=%s target=%s",
        subsystem,
        status,
        pages if keep else _count_raw_pages(root, subsystem),
        keep,
        run.target or "all",
    )
    summary["endpoint_states"] = _persist_endpoint_states(root, subsystem)
    return summary, failure_ledger, status


_LOG_HEAD_LINES = 200
_LOG_TAIL_LINES = 200


class _HeadTailLines:
    """Keeps the first N and last N lines of a run instead of just the tail.

    A plain deque(maxlen=...) evicts from the front, so an early-cycle failure
    (e.g. account 3 of 50) gets silently dropped from FetchRun.log_excerpt once
    a long run produces enough later output. Keeping both ends means the start
    of the run survives even when the run runs long.
    """

    def __init__(self, head: int = _LOG_HEAD_LINES, tail: int = _LOG_TAIL_LINES) -> None:
        self._head: list[str] = []
        self._tail: deque[str] = deque(maxlen=tail)
        self._head_limit = head
        self._total = 0

    def append(self, line: str) -> None:
        self._total += 1
        if len(self._head) < self._head_limit:
            self._head.append(line)
        else:
            self._tail.append(line)

    def __iter__(self):
        omitted = self._total - len(self._head) - len(self._tail)
        yield from self._head
        if omitted > 0:
            yield f"... [{omitted} line(s) omitted] ..."
        yield from self._tail


def _kill_process_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()


def _await_process(
    process: subprocess.Popen, *, timeout: float, subsystem: str,
    literals: list[str] | None = None,
) -> tuple[int, "_HeadTailLines"]:
    # Redact once, here, as each line is read: this is the single point every
    # line passes through on its way to both the Docker log and FetchRun.
    # log_excerpt, so neither can carry a secret that the other filtered.
    literals = literals or []

    def _clean(raw: str) -> str:
        return redact_text(raw.rstrip(), literals=literals)

    lines = _HeadTailLines()
    stdout = process.stdout
    use_select = False
    if stdout is not None and hasattr(stdout, "fileno"):
        try:
            stdout.fileno()
            use_select = True
        except (AttributeError, OSError, ValueError):
            use_select = False
    deadline = time.monotonic() + max(float(timeout), 1.0)
    if use_select:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("fetcher[%s] wall-clock timeout; killing process group", subsystem)
                _kill_process_group(process)
                try:
                    return int(process.wait(timeout=5) or -9), lines
                except subprocess.TimeoutExpired:
                    return -9, lines
            ready, _, _ = select.select([stdout], [], [], min(1.0, remaining))
            if ready:
                line = stdout.readline()
                if not line:
                    break
                clean = _clean(line)
                lines.append(clean)
                logger.info("fetcher[%s] %s", subsystem, clean)
            elif process.poll() is not None:
                break
        code = process.poll()
        if code is None:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                logger.warning("fetcher[%s] wall-clock timeout; killing process group", subsystem)
                _kill_process_group(process)
                code = process.wait(timeout=5) or -9
        return int(code), lines
    for line in stdout or ():
        clean = _clean(str(line))
        lines.append(clean)
        logger.info("fetcher[%s] %s", subsystem, clean)
    return int(process.wait()), lines


def _persist_session(root: Path) -> None:
    """Copy refreshed scratch cookies/headers back onto the durable XSession.

    Live/historical/search now run as separate concurrent worker processes
    (see docker-compose.yml queues), so multiple subprocesses can have the
    session checked out at once. Only write fields this run actually changed
    from what it started with (via the snapshot _write_config took) -- an
    unconditional write here would let a run that never refreshed clobber a
    concurrent run's genuine refresh with stale cookies, since whichever
    finishes last always wins. No snapshot on disk (e.g. a scratch dir built
    without _write_config) falls back to the old unconditional-write behavior.
    """
    config = _read_json(root / "config" / "config.json", {})
    if not isinstance(config, dict):
        return
    snapshot = _read_json(root / "config" / "_session_snapshot.json", None)
    cookies = config.get("api_cookies")
    headers = config.get("api_headers")
    if isinstance(snapshot, dict):
        if isinstance(cookies, dict) and cookies == snapshot.get("api_cookies"):
            cookies = None
        if isinstance(headers, dict) and headers == snapshot.get("api_headers"):
            headers = None
    session = _active_session()
    if session is None:
        return
    fields = []
    if isinstance(cookies, dict) and cookies:
        session.cookies = cookies
        fields.append("cookies")
    if isinstance(headers, dict) and headers:
        session.headers = headers
        fields.append("headers")
    if fields:
        session.save(update_fields=[*fields, "updated_at"])


SCRATCH_PREFIX = "tdf_run_"


def sweep_stale_scratch_dirs() -> int:
    """Delete abandoned scratch dirs, which hold live X cookies and a bearer token.

    cleanup() runs in the caller's `finally`, which a SIGKILL skips -- and the
    worker has a 2g memory limit with chromium in the image, so OOM-kill is a
    realistic trigger. Anything older than the cycle ceiling cannot belong to a
    live run.
    """
    cutoff = time.time() - (settings.FETCH_CYCLE_TIMEOUT_SECONDS + 300)
    removed = 0
    for path in Path(tempfile.gettempdir()).glob(f"{SCRATCH_PREFIX}*"):
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.warning("swept %d abandoned fetcher scratch dir(s)", removed)
    return removed


def run_fetcher(
    module: str,
    args: list[str],
    subsystem: str,
    searches: list | None = None,
    *,
    target: str = "",
    task_id: str = "",
) -> FetcherRunResult:
    """Run a canonical pipeline and return its scratch root plus durable run row.

    Caller ingests from the returned root, then calls cleanup(root). On any
    internal failure (config write, state restore, persist) the scratch dir is
    cleaned up here so it never leaks. A non-zero subprocess exit is logged but
    does NOT raise — the engine may have written partial results before failing,
    and those are still worth ingesting.
    """
    run = FetchRun.objects.create(
        run_id=f"saas_{uuid.uuid4().hex}",
        task_id=task_id,
        subsystem=subsystem,
        target=target,
    )
    root = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
    try:
        config_path = _write_config(root, searches=searches)
        _restore_state(root, subsystem)

        env = dict(os.environ)
        env["TDF_PROJECT_ROOT"] = str(root)
        env["TDF_CONFIG"] = str(config_path)
        # Archive-walk budget. The engine runs as a subprocess, so these cross the
        # boundary as env rather than settings imports; naming them here keeps the
        # knob a single .env entry instead of two independent defaults.
        env["TDF_HISTORICAL_PAGES_PER_TICK"] = str(settings.FETCH_HISTORICAL_PAGES_PER_TICK)
        env["TDF_HISTORICAL_QUOTA_FLOOR"] = str(settings.FETCH_HISTORICAL_QUOTA_FLOOR)
        env["TDF_ARCHIVE_EARLIEST_DATE"] = settings.FETCH_ARCHIVE_EARLIEST_DATE
        # cwd is the scratch root, so point the subprocess at this project for
        # the engine package (it ships here as `fetcher/`).
        env["PYTHONPATH"] = str(settings.BASE_DIR) + os.pathsep + env.get("PYTHONPATH", "")

        # Snapshot the secrets before the run: auth refresh can rotate XSession
        # mid-run, and the log may quote either the old or the new value.
        session_literals = _literal_secrets()
        process = subprocess.Popen(
            [sys.executable, "-m", module, *args],
            env=env,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        return_code, lines = _await_process(
            process,
            timeout=settings.FETCH_CYCLE_TIMEOUT_SECONDS,
            subsystem=subsystem,
            literals=session_literals,
        )
        _persist_state(root, subsystem)
        _persist_session(root)
        summary, failure_ledger, status = _persist_artifacts(root, subsystem, run, return_code)
        run.return_code = return_code
        run.log_excerpt = "\n".join(lines)[-20000:]
        run.summary = summary
        run.failure_ledger = failure_ledger
        run.status = status
        run.save(update_fields=["return_code", "log_excerpt", "summary", "failure_ledger", "status"])
        if return_code != 0:
            logger.warning(
                "fetcher %s exited with code %s for subsystem=%s; "
                "ingesting whatever partial results exist",
                module,
                return_code,
                subsystem,
            )
        return FetcherRunResult(root=root, run=run)
    except Exception:
        # Never leak the scratch dir if something inside the try block raised
        # before the caller could take ownership of ``root``.
        shutil.rmtree(root, ignore_errors=True)
        run.status = "failed"
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "finished_at"])
        raise


def cleanup(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)


def finalize_run(
    run: FetchRun,
    *,
    ingested_tweets: int = 0,
    new_tweets: int | None = None,
    task_failed: bool = False,
) -> None:
    summary = dict(run.summary or {})
    summary["ingested_tweets"] = int(ingested_tweets)
    # How many of those rows the archive had never seen. Without it a repoll that
    # re-stored the same 40 hits reported "40 results stored", indistinguishable
    # from a run that actually found 40 things. Defaults to the ingested count
    # only when the caller genuinely cannot tell them apart.
    summary["new_tweets"] = int(
        ingested_tweets if new_tweets is None else new_tweets
    )
    run.summary = summary
    if task_failed:
        run.status = "failed"
    run.finished_at = timezone.now()
    run.save(update_fields=["summary", "status", "finished_at"])


def iter_processed_tweets(root: Path, subsystem: str) -> Iterable[dict]:
    """Yield normalized tweet dicts from the complete per-account set of a run.

    The historical/live engine writes seven processed sets per account
    (1_user_tweets .. 7_symmetric_difference); ``4_union`` is the complete
    superset, so reading only it avoids re-ingesting the six subset dirs.
    """
    sub = "historical_live" if subsystem in ("historical", "live") else subsystem
    union = root / "data" / sub / "processed" / "4_union"
    if not union.exists():
        return
    for json_file in union.rglob("*.json"):
        try:
            payload = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    yield item


def iter_search_tweets(root: Path, slug: str, product: str = "") -> Iterable[dict]:
    """Yield tweets from a search run's export ({slug}.json with a tweets key).

    ``slug`` is normalized the same way SearchQueryBuilder does it,
    because the engine writes processed exports under the normalized slug, not
    the raw model slug. Scope to ``product`` so Top/Latest do not mix.
    """
    norm = normalize_slug(slug)
    processed = root / "data" / "search" / "processed" / norm
    if product:
        processed = processed / str(product).lower()
    if not processed.exists():
        return
    for json_file in processed.rglob(f"{norm}.json"):
        try:
            payload = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tweets = payload.get("tweets") if isinstance(payload, dict) else None
        if isinstance(tweets, list):
            for item in tweets:
                if isinstance(item, dict):
                    yield item
