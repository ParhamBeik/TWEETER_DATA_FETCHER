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

from apps.tweets.models import EndpointState, FetchRun, KeyValueState, RawPage, XSession

from .accounts import tracked_accounts_payload

logger = logging.getLogger(__name__)

SEED_DIR = Path(settings.BASE_DIR) / "seed"
FETCHER_SRC = next(
    (
        path
        for path in (
            Path(os.environ["TDF_FETCHER_SRC"]) if os.environ.get("TDF_FETCHER_SRC") else None,
            Path("/app/fetcher"),
            Path(settings.BASE_DIR).parents[1] / "twitter_fetcher" / "src",
        )
        if path is not None and path.is_dir()
    ),
    Path("/app/fetcher"),
)

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
        "pagination_depth": 1,
        "max_retries": 3,
        "rolling_hours": 24,
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
        headers = {str(key): str(value) for key, value in session.headers.items() if value}
        base["api_cookies"] = {str(key): str(value) for key, value in session.cookies.items() if value}
        base["api_headers"] = headers
        authorization = next((value for key, value in headers.items() if key.lower() == "authorization"), "")
        if authorization.lower().startswith("bearer "):
            base.setdefault("api_auth", {})["bearer_token"] = authorization[7:].strip()
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


def _persist_raw_pages(root: Path, subsystem: str, run: FetchRun) -> int:
    sub = "historical_live" if subsystem in ("historical", "live") else subsystem
    raw_root = root / "data" / sub / "raw"
    count = 0
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
        RawPage.objects.update_or_create(
            endpoint=endpoint,
            account=account,
            batch=batch,
            page_number=int(match.group(1)),
            defaults={"payload": payload, "fetch_run": run},
        )
        count += 1
    cutoff = timezone.now() - timedelta(days=7)
    while True:
        stale_ids = list(
            RawPage.objects.filter(created_at__lt=cutoff)
            .order_by("id")
            .values_list("id", flat=True)[:500]
        )
        if not stale_ids:
            break
        RawPage.objects.filter(id__in=stale_ids).delete()
    return count


def _collect_run_summary(root: Path, subsystem: str, return_code: int) -> tuple[dict, dict, str]:
    sub = "historical_live" if subsystem in ("historical", "live") else subsystem
    base = root / "data" / sub
    event_counts: Counter[str] = Counter()
    recent_events = []
    for event_file in (base / "logs").rglob("events.jsonl") if (base / "logs").exists() else ():
        for line in event_file.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            event_counts[str(event.get("type") or "unknown")] += 1
            recent_events.append(event)
    recent_events = recent_events[-100:]

    http_summary = _read_json(base / "logs" / "http_summary.json", {})
    failure_ledger = http_summary.get("failure_ledger", {}) if isinstance(http_summary, dict) else {}
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

    by_status = http_summary.get("by_status_code", {}) if isinstance(http_summary, dict) else {}
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
        "recent_events": recent_events,
        "reports": report_summaries,
        "status_counts": dict(status_counts),
        **({"status_reason": "no_reports_written"} if no_evidence else {}),
    }, failure_ledger, status


def _persist_artifacts(root: Path, subsystem: str, run: FetchRun, return_code: int) -> tuple[dict, dict, str]:
    summary, failure_ledger, status = _collect_run_summary(root, subsystem, return_code)
    summary["raw_pages"] = _persist_raw_pages(root, subsystem, run)
    summary["endpoint_states"] = _persist_endpoint_states(root, subsystem)
    return summary, failure_ledger, status


def _kill_process_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()


def _await_process(
    process: subprocess.Popen, *, timeout: float, subsystem: str
) -> tuple[int, deque[str]]:
    lines: deque[str] = deque(maxlen=400)
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
                clean = line.rstrip()
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
        clean = str(line).rstrip()
        lines.append(clean)
        logger.info("fetcher[%s] %s", subsystem, clean)
    return int(process.wait()), lines


def _persist_session(root: Path) -> None:
    """Copy refreshed scratch cookies/headers back onto the durable XSession."""
    config = _read_json(root / "config" / "config.json", {})
    if not isinstance(config, dict):
        return
    session = _active_session()
    if session is None:
        return
    fields = []
    cookies = config.get("api_cookies")
    headers = config.get("api_headers")
    if isinstance(cookies, dict) and cookies:
        session.cookies = cookies
        fields.append("cookies")
    if isinstance(headers, dict) and headers:
        session.headers = headers
        fields.append("headers")
    if fields:
        session.save(update_fields=[*fields, "updated_at"])


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
    root = Path(tempfile.mkdtemp(prefix="tdf_run_"))
    try:
        config_path = _write_config(root, searches=searches)
        _restore_state(root, subsystem)

        env = dict(os.environ)
        env["TDF_PROJECT_ROOT"] = str(root)
        env["TDF_CONFIG"] = str(config_path)
        env["PYTHONPATH"] = str(FETCHER_SRC) + os.pathsep + env.get("PYTHONPATH", "")

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
            process, timeout=settings.FETCH_CYCLE_TIMEOUT_SECONDS, subsystem=subsystem
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


def finalize_run(run: FetchRun, *, ingested_tweets: int = 0, task_failed: bool = False) -> None:
    summary = dict(run.summary or {})
    summary["ingested_tweets"] = int(ingested_tweets)
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
