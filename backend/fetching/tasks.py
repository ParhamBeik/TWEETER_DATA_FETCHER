"""Celery tasks: run the fetcher and ingest into Postgres."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import timedelta
from typing import Callable, Optional

from celery import current_task, shared_task
from django.conf import settings
from django.core.cache import cache
from django.db.models import Exists, OuterRef
from django.utils import timezone

from tweets.models import EndpointState, FetchRun, Search, SearchResult, Tweet, TwitterUser

from . import runner
from .accounts import interval_for, median_gap_seconds, sync_quarantine_from_live_state
from .ingest import ingest_search_results, ingest_tweets

logger = logging.getLogger(__name__)

HISTORICAL_MODULE = "fetcher.historical"
LIVE_MODULE = "fetcher.live"
SEARCH_MODULE = "fetcher.search"


def _task_id() -> str:
    return str(getattr(getattr(current_task, "request", None), "id", "") or "")


@contextmanager
def _cycle_lock(name: str):
    """Skip overlapping beat ticks. Release in finally; TTL is only a crash backstop.

    The TTL must outlive the work it guards, so it is the subprocess wall-clock
    ceiling -- never the beat interval. A beat-interval TTL expires mid-cycle and
    lets the next tick start a second fetcher against the shared X session.
    """
    key = f"fetch_cycle_lock:{name}"
    acquired = cache.add(key, "1", timeout=settings.FETCH_CYCLE_TIMEOUT_SECONDS + 60)
    try:
        yield acquired
    finally:
        if acquired:
            cache.delete(key)


def _run_and_ingest(module: str, args: list[str], subsystem: str, target: str) -> int:
    result = runner.run_fetcher(module, args, subsystem, target=target, task_id=_task_id())
    count = 0
    failed = False
    try:
        count = ingest_tweets(runner.iter_processed_tweets(result.root, subsystem))
        return count
    except Exception:
        failed = True
        raise
    finally:
        runner.finalize_run(result.run, ingested_tweets=count, task_failed=failed)
        if subsystem == "live":
            sync_quarantine_from_live_state()
        runner.cleanup(result.root)


def _run_cycle(
    module: str,
    args: list[str],
    subsystem: str,
    *,
    target: str = "all",
    searches: list | None = None,
    on_complete: Optional[Callable[["FetchRun"], None]] = None,
) -> int:
    result = runner.run_fetcher(
        module, args, subsystem, searches=searches, target=target, task_id=_task_id()
    )
    count = 0
    failed = False
    try:
        if subsystem == "search":
            for search in searches or []:
                count += ingest_search_results(
                    search, runner.iter_search_tweets(result.root, search.slug, search.product)
                )
                # Stamped on every attempt, not only a completed one. This is what
                # dispatch_due_searches schedules from, so leaving it unset after a
                # partial run would re-queue that search on every dispatcher tick
                # and spend a browser bootstrap every few minutes on the one query
                # least able to finish. A partial run waits its normal interval.
                search.last_run_at = timezone.now()
                search.save(update_fields=["last_run_at"])
        else:
            count = ingest_tweets(runner.iter_processed_tweets(result.root, subsystem))
        return count
    except Exception:
        failed = True
        raise
    finally:
        runner.finalize_run(result.run, ingested_tweets=count, task_failed=failed)
        if subsystem == "live":
            sync_quarantine_from_live_state()
        if on_complete is not None:
            on_complete(result.run)
        runner.cleanup(result.root)


@shared_task(name="fetching.tasks.fetch_account_historical")
def fetch_account_historical(handle: str) -> int:
    # Locked per handle: this endpoint is user-triggerable, and every run spends
    # the one shared X session's rate budget. Repeat requests collapse.
    with _cycle_lock(f"fetch_account_historical:{handle}") as acquired:
        if not acquired:
            logger.warning("fetch_account_historical(%s): already running, skipped", handle)
            return 0
        return _run_and_ingest(HISTORICAL_MODULE, ["--only", handle], "historical", handle)


@shared_task(name="fetching.tasks.fetch_account_live")
def fetch_account_live(handle: str) -> int:
    with _cycle_lock(f"fetch_account_live:{handle}") as acquired:
        if not acquired:
            logger.warning("fetch_account_live(%s): already running, skipped", handle)
            return 0
        return _run_and_ingest(LIVE_MODULE, ["--once", "--account", handle], "live", handle)


@shared_task(name="fetching.tasks.poll_live_all")
def poll_live_all() -> int:
    with _cycle_lock("poll_live_all") as acquired:
        if not acquired:
            logger.warning("poll_live_all: skipped overlapping cycle")
            return 0
        reap_orphaned_fetch_runs()
        return _run_cycle(LIVE_MODULE, ["--once"], "live")


def _archive_state() -> dict[str, dict]:
    """Per-account archive-walk state, keyed by lowercased handle.

    The engine normalizes handles to lowercase when it writes sync state; the
    TwitterUser table preserves the display casing. Joining in Python beats an
    iexact subquery for a fleet this size and keeps one obvious mapping.
    """
    return {
        str(row.account).lower(): (row.data if isinstance(row.data, dict) else {})
        for row in EndpointState.objects.filter(endpoint="UserTweets")
    }


def _backfill_queue(limit: int) -> list[str]:
    """Tracked accounts whose archive walk is unfinished, most deserving first.

    Ordered by how many ticks an account has failed to make progress (so a stuck
    account sinks instead of blocking the queue), then by tier, then by id. An
    account is dropped for good once its walk reaches the end of the timeline.
    """
    archive = _archive_state()
    pending = [
        user
        for user in TwitterUser.objects.filter(tracking=True, quarantined=False).order_by("priority", "id")
        if not archive.get(user.handle.lower(), {}).get("backfill_complete")
    ]
    pending.sort(
        key=lambda user: (
            int(archive.get(user.handle.lower(), {}).get("backfill_stalled_ticks", 0) or 0),
            int(user.priority or 7),
            user.id,
        )
    )
    return [user.handle for user in pending[:limit]]


@shared_task(name="fetching.tasks.backfill_historical_all")
def backfill_historical_all() -> int:
    """Advance the archive walk for one bounded chunk of accounts per tick.

    The backfill is a finite job: each account's timeline is walked backwards
    once, resuming from its own stored cursor, until it runs out of tweets. A
    tick is a bite of that walk (FETCH_HISTORICAL_PAGES_PER_TICK pages), small
    enough to finish well inside FETCH_CYCLE_TIMEOUT_SECONDS on the solo
    worker, and it always leaves FETCH_HISTORICAL_QUOTA_FLOOR requests in the
    shared bucket for live polling.

    This replaced a version that re-walked each account from page 1 every tick
    and could only advance its queue on a fully "completed" run. An account
    whose timeline ended without the API ever withholding a cursor could never
    report completed, so it was refetched forever and the 67 accounts behind it
    were never reached. Completion is now proven per account by the engine
    (see fetcher.historical._record_backfill_progress) rather than inferred
    from the chunk's run status.
    """
    with _cycle_lock("backfill_historical_all") as acquired:
        if not acquired:
            logger.warning("backfill_historical_all: skipped overlapping cycle")
            return 0
        reap_orphaned_fetch_runs()

        handles = _backfill_queue(settings.FETCH_HISTORICAL_CHUNK_SIZE)
        if not handles:
            logger.info("backfill_historical_all: every tracked account is fully archived")
            return 0

        def _mark_chunk(_run: FetchRun) -> None:
            archive = _archive_state()
            for handle in handles:
                state = archive.get(handle.lower(), {})
                pages = state.get("backfill_pages_done", 0)
                if state.get("backfill_complete"):
                    TwitterUser.objects.filter(handle=handle).update(
                        historical_backfilled_at=timezone.now()
                    )
                    logger.info(
                        "backfill_historical_all: @%s fully archived after %s page(s)", handle, pages,
                    )
                else:
                    logger.info(
                        "backfill_historical_all: @%s at %s page(s), outcome=%s, resumes next tick",
                        handle, pages, state.get("backfill_last_outcome"),
                    )

        return _run_cycle(
            HISTORICAL_MODULE,
            ["--only", ",".join(handles)],
            "historical",
            target=f"chunk:{len(handles)}",
            on_complete=_mark_chunk,
        )


@shared_task(name="fetching.tasks.run_search")
def run_search(search_id: int) -> int:
    """Run one saved search, alone, with the whole cycle budget to itself."""
    search = Search.objects.filter(id=search_id).first()
    if search is None:
        return 0
    # Locked per search: dispatch_due_searches and a manual trigger can both
    # reach this, and two browser bootstraps against the one shared X session
    # is exactly the race _cycle_lock exists to prevent.
    with _cycle_lock(f"run_search:{search_id}") as acquired:
        if not acquired:
            logger.warning("run_search(%s): already running, skipped", search.slug)
            return 0
        return _run_cycle(
            SEARCH_MODULE,
            ["--once", "--only", search.slug],
            "search",
            target=f"{search.slug}:{search.product}",
            searches=[search],
        )


@shared_task(name="fetching.tasks.dispatch_due_searches")
def dispatch_due_searches() -> int:
    """Queue each enabled search that is due, on its own interval.

    Replaces a fleet-wide cycle that ran every enabled search back-to-back in
    one subprocess under one FETCH_CYCLE_TIMEOUT_SECONDS. Deep search pages come
    from browser scrolling, minutes per query, so the budget ran out partway
    through and the last query in the list was SIGKILLed on every single cycle
    -- it never once completed. One task per search means each gets the full
    budget, and cadence lives in the DB instead of the beat schedule.
    """
    now = timezone.now()
    queued = 0
    for search in Search.objects.filter(enabled=True):
        due_at = search.last_run_at + timedelta(seconds=search.interval_seconds) if search.last_run_at else None
        if due_at is not None and due_at > now:
            continue
        run_search.delay(search.id)
        queued += 1
    if queued:
        logger.info("dispatch_due_searches: queued %d search(es)", queued)
    return queued


@shared_task(name="fetching.tasks.repoll_searches")
def repoll_searches() -> int:
    """Run every enabled search in one cycle.

    No longer scheduled -- dispatch_due_searches supersedes it -- but kept
    registered and callable: the task name is a wire identifier, and dropping it
    would strand any message already queued under it.
    """
    with _cycle_lock("repoll_searches") as acquired:
        if not acquired:
            logger.warning("repoll_searches: skipped overlapping cycle")
            return 0
        reap_orphaned_fetch_runs()
        searches = list(Search.objects.filter(enabled=True))
        if not searches:
            return 0
        return _run_cycle(SEARCH_MODULE, ["--once"], "search", searches=searches)


@shared_task(name="fetching.tasks.recompute_poll_intervals")
def recompute_poll_intervals() -> int:
    """Re-derive each account's polling cadence from how often it actually posts.

    A fixed per-tier interval spends the same quota on an account that posts
    forty times a day and one that posts twice a week. Measuring the real rate
    and clamping it into the tier's band spends the budget where there is
    something to collect, while keeping importance in charge at the edges.
    """
    since = timezone.now() - timedelta(days=settings.FETCH_INTERVAL_SAMPLE_DAYS)
    updated = 0
    for user in TwitterUser.objects.filter(tracking=True):
        times = list(
            Tweet.objects.filter(
                account__iexact=user.handle, created_at__gte=since, created_at__isnull=False
            ).values_list("created_at", flat=True)
        )
        gap = median_gap_seconds(times)
        interval = interval_for(user.priority, gap)
        if (user.poll_interval_seconds, user.observed_median_gap_seconds) == (interval, gap):
            continue
        user.poll_interval_seconds = interval
        user.observed_median_gap_seconds = gap
        user.save(update_fields=["poll_interval_seconds", "observed_median_gap_seconds"])
        updated += 1
        logger.info(
            "recompute_poll_intervals: @%s tier=%s median_gap=%ss -> poll every %ss",
            user.handle, user.priority, gap, interval,
        )
    return updated


@shared_task(name="fetching.tasks.purge_expired_search_tweets")
def purge_expired_search_tweets() -> int:
    """Drop search-only tweets older than SEARCH_TWEET_TTL_DAYS."""
    cutoff = timezone.now() - timedelta(days=settings.SEARCH_TWEET_TTL_DAYS)
    tracked = TwitterUser.objects.filter(tracking=True).values_list("handle", flat=True)
    has_search = Exists(SearchResult.objects.filter(tweet_id=OuterRef("pk")))
    qs = Tweet.objects.filter(has_search, ingested_at__lt=cutoff).exclude(
        account__in=list(tracked)
    )
    deleted, _ = qs.delete()
    return deleted


def reap_orphaned_fetch_runs() -> int:
    """Fail runs whose worker died, and sweep the scratch dirs they left behind.

    A SIGKILLed worker skips the cleanup() in the caller's finally, stranding a
    /tmp dir that contains live X cookies and a bearer token.
    """
    runner.sweep_stale_scratch_dirs()
    cutoff = timezone.now() - timedelta(seconds=settings.FETCH_CYCLE_TIMEOUT_SECONDS)
    return FetchRun.objects.filter(status="running", started_at__lt=cutoff).update(
        status="failed", finished_at=timezone.now()
    )


@shared_task(name="fetching.tasks.purge_old_fetch_runs")
def purge_old_fetch_runs() -> int:
    reap_orphaned_fetch_runs()
    cutoff = timezone.now() - timedelta(days=settings.FETCH_RUN_RETENTION_DAYS)
    deleted, _ = FetchRun.objects.filter(started_at__lt=cutoff).delete()
    return deleted
