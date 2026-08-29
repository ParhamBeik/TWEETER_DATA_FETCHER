"""Celery tasks: run the fetcher and ingest into Postgres."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import timedelta
from typing import Callable, Optional

from celery import current_task, shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from tweets.models import FetchRun, RawPage, Search, SearchHit, SearchTweet, Tweet, TwitterUser

from . import runner
from .accounts import (
    archive_progress,
    archive_state,
    interval_for,
    median_gap_seconds,
    sync_quarantine_from_live_state,
)
from .ingest import ingest_search_hits, ingest_tweets

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


def _claim_search(search: Search, now) -> bool:
    """Win the right to queue this search, atomically, or leave it to the winner.

    A compare-and-swap on the row the dispatcher already reads: the UPDATE only
    matches while last_run_at still holds the value this tick saw, so exactly one
    of N concurrent or back-to-back ticks can queue a given search. Stamping at
    dispatch (rather than only when the task runs) is what makes it a claim --
    the search stops being "due" the moment it is queued, not 5-15 minutes later
    when the fetch finishes.

    This is what the dispatcher amplification needed. It is cheap and used to
    share a serialized queue with the fetches it schedules, so its ticks backed
    up behind them; when ~100 drained in a row, each read the same due rows and
    queued them again -- 201 stacked dispatcher messages and 77 duplicate
    fetches for two searches, while the other four went 16 hours without running.

    Deliberately a database CAS rather than a Redis marker: there is no TTL to
    size (a queue wait has no bounded duration to guess at), no second source of
    truth to drift, and it survives a worker crash, a redeploy and a cache flush.
    A task lost in flight simply waits one interval -- already this codebase's
    chosen semantic for a run that did not finish.
    """
    return bool(
        Search.objects.filter(id=search.id, last_run_at=search.last_run_at).update(
            last_run_at=now
        )
    )


def _run_and_ingest(module: str, args: list[str], subsystem: str, target: str) -> int:
    result = runner.run_fetcher(module, args, subsystem, target=target, task_id=_task_id())
    count = 0
    failed = False
    try:
        count = ingest_tweets(runner.iter_processed_tweets(result.root, subsystem), subsystem)
        return count
    except Exception:
        failed = True
        raise
    finally:
        runner.finalize_run(
            result.run,
            ingested_tweets=count,
            new_tweets=getattr(count, "new", None),
            task_failed=failed,
        )
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
    new_count = 0
    failed = False
    try:
        if subsystem == "search":
            # One search per run since dispatch_due_searches took over, but the
            # signature still accepts a list for repoll_searches. Attributing the
            # run to a single search is what makes "this phrase's history" a
            # relation rather than a parse of `target`.
            if len(searches or []) == 1:
                FetchRun.objects.filter(pk=result.run.pk).update(search=searches[0])
            for search in searches or []:
                ingested = ingest_search_hits(
                    search,
                    runner.iter_search_tweets(result.root, search.slug, search.product),
                )
                count += ingested
                new_count += ingested.new
                # Stamped on every attempt, not only a completed one. This is what
                # dispatch_due_searches schedules from, so leaving it unset after a
                # partial run would re-queue that search on every dispatcher tick
                # and spend a browser bootstrap every few minutes on the one query
                # least able to finish. A partial run waits its normal interval.
                search.last_run_at = timezone.now()
                search.save(update_fields=["last_run_at"])
        else:
            count = ingest_tweets(
                runner.iter_processed_tweets(result.root, subsystem), subsystem
            )
            new_count = count.new
        return count
    except Exception:
        failed = True
        raise
    finally:
        runner.finalize_run(
            result.run, ingested_tweets=count, new_tweets=new_count, task_failed=failed
        )
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
    """Per-account archive-walk state, keyed by lowercased handle."""
    return archive_state()


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
            # Say what is actually true. The queue also skips quarantined
            # accounts, and an account can be done because X refused to page
            # deeper rather than because we reached its first tweet -- reporting
            # either as "fully archived" is how the gap stayed invisible.
            progress = archive_progress()
            blocked = TwitterUser.objects.filter(tracking=True, quarantined=True).count()
            logger.info(
                "backfill_historical_all: nothing to walk -- %d fully archived, "
                "%d stopped at X's serving depth, %d quarantined",
                len(progress["complete"]), len(progress["depth_limited"]), blocked,
            )
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
        # The query was deleted between dispatch and pickup. Nothing to do, and
        # nothing to complain about -- teardown_search revokes what it can, and
        # this is the backstop for the message it could not.
        return 0
    # The claim is spent the moment the work starts: leaving the id set would let
    # a delete try to revoke a task that is already running, and would keep the
    # console showing "queued" for the whole run.
    Search.objects.filter(id=search_id).update(queued_task_id="")
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

    Being due is necessary but not sufficient: a search that already has a task
    waiting is skipped, so a burst of dispatcher ticks queues each search at
    most once (see _claim_search). This also bounds the queue when the fleet is
    oversubscribed -- six searches on a 30-minute cadence against runs that take
    5-15 minutes each cannot all keep their nominal interval, and the honest
    degradation is "each runs a little late", not "the queue grows forever".
    """
    now = timezone.now()
    queued = 0
    for search in Search.objects.filter(enabled=True):
        due_at = search.last_run_at + timedelta(seconds=search.interval_seconds) if search.last_run_at else None
        if due_at is not None and due_at > now:
            continue
        if not _claim_search(search, now):
            logger.info(
                "dispatch_due_searches: %s is due but already queued, not re-queuing",
                search.slug,
            )
            continue
        result = run_search.delay(search.id)
        # Stamped after the claim, not instead of it: the CAS is what prevents
        # double-queuing, this only records *which* message won so a delete can
        # revoke it. A crash between the two leaves the search looking idle for
        # one interval, which is already the semantic for a run that did not
        # finish.
        Search.objects.filter(id=search.id).update(queued_task_id=str(result.id or ""))
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
    """Drop search hits older than SEARCH_TWEET_TTL_DAYS.

    Simpler than it used to be, and that is the point of the split. The old
    version had to exclude tracked handles by hand, because search results and
    the tracked-account archive shared one table and one row could belong to
    both; a search hit now lives in its own table with its own clock, so the
    tracked archive cannot be caught by this.

    Bodies go when their last hit does -- a tweet two queries both matched
    survives until neither wants it.

    Task name= is unchanged: it is a wire identifier and renaming one strands any
    message already queued under the old name.
    """
    cutoff = timezone.now() - timedelta(days=settings.SEARCH_TWEET_TTL_DAYS)
    SearchHit.objects.filter(last_seen_at__lt=cutoff).delete()
    deleted, _ = SearchTweet.objects.filter(
        ingested_at__lt=cutoff, hits__isnull=True
    ).delete()
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


@shared_task(name="fetching.tasks.purge_old_raw_pages")
def purge_old_raw_pages() -> int:
    """Expire raw GraphQL pages on their own, shorter clock.

    They are 91% of the database and grow ~750 MB/day, so they cannot wait for
    the 90-day run retention -- that is ~65 GB of an 80 GB margin. Their value
    also decays much faster than a run record's: a raw page is for re-processing
    or debugging a recent fetch, while the run row stays cheap and useful.

    Deleted by age rather than via the run FK because a page can outlive its
    run: FetchRun rows are purged on their own schedule, and pages written by a
    run that was already reaped would otherwise have no clock at all.
    """
    cutoff = timezone.now() - timedelta(days=settings.RAW_PAGE_RETENTION_DAYS)
    total = 0
    # Chunked: a single unbounded DELETE over millions of JSONB rows holds one
    # long transaction and bloats WAL on a 1 GB container. Also capped per run,
    # because the first pass after deploy has a multi-GB backlog to clear and
    # this shares a worker -- an unbounded loop would hold it for however long
    # that takes. Whatever is left simply expires on tomorrow's run.
    while total < settings.RAW_PAGE_PURGE_MAX_ROWS:
        ids = list(
            RawPage.objects.filter(created_at__lt=cutoff).values_list("pk", flat=True)[:5000]
        )
        if not ids:
            break
        deleted, _ = RawPage.objects.filter(pk__in=ids).delete()
        total += deleted
    if total:
        logger.info("purge_old_raw_pages: deleted %d raw page(s)", total)
    return total


@shared_task(name="fetching.tasks.archive_media")
def archive_media() -> int:
    """Copy a small batch of tweet photos onto the media volume.

    Chunked on purpose: this shares the solo control worker with the search
    dispatcher. One giant download of the backlog would starve dispatch the
    same way a shared search queue once did.
    """
    from fetching.media import archive_batch

    return archive_batch(settings.MEDIA_ARCHIVE_BATCH)
