"""Celery tasks: run the fetcher and ingest into Postgres."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import timedelta

from celery import current_task, shared_task
from django.conf import settings
from django.core.cache import cache
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.tweets.models import FetchRun, Search, SearchResult, Tweet, TwitterUser

from . import runner
from .accounts import sync_quarantine_from_live_state
from .ingest import ingest_search_results, ingest_tweets

logger = logging.getLogger(__name__)

HISTORICAL_MODULE = "tweeter_data_fetcher.pipelines.historical.service"
LIVE_MODULE = "tweeter_data_fetcher.pipelines.live.service"
SEARCH_MODULE = "tweeter_data_fetcher.pipelines.search.service"


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
                if result.run.status == "completed":
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
        runner.cleanup(result.root)


@shared_task(name="apps.fetching.tasks.fetch_account_historical")
def fetch_account_historical(handle: str) -> int:
    # Locked per handle: this endpoint is user-triggerable, and every run spends
    # the one shared X session's rate budget. Repeat requests collapse.
    with _cycle_lock(f"fetch_account_historical:{handle}") as acquired:
        if not acquired:
            logger.warning("fetch_account_historical(%s): already running, skipped", handle)
            return 0
        return _run_and_ingest(HISTORICAL_MODULE, ["--only", handle], "historical", handle)


@shared_task(name="apps.fetching.tasks.fetch_account_live")
def fetch_account_live(handle: str) -> int:
    with _cycle_lock(f"fetch_account_live:{handle}") as acquired:
        if not acquired:
            logger.warning("fetch_account_live(%s): already running, skipped", handle)
            return 0
        return _run_and_ingest(LIVE_MODULE, ["--once", "--account", handle], "live", handle)


@shared_task(name="apps.fetching.tasks.poll_live_all")
def poll_live_all() -> int:
    with _cycle_lock("poll_live_all") as acquired:
        if not acquired:
            logger.warning("poll_live_all: skipped overlapping cycle")
            return 0
        reap_orphaned_fetch_runs()
        return _run_cycle(LIVE_MODULE, ["--once"], "live")


@shared_task(name="apps.fetching.tasks.backfill_historical_all")
def backfill_historical_all() -> int:
    with _cycle_lock("backfill_historical_all") as acquired:
        if not acquired:
            logger.warning("backfill_historical_all: skipped overlapping cycle")
            return 0
        reap_orphaned_fetch_runs()
        return _run_cycle(HISTORICAL_MODULE, [], "historical")


@shared_task(name="apps.fetching.tasks.run_search")
def run_search(search_id: int) -> int:
    search = Search.objects.filter(id=search_id).first()
    if search is None:
        return 0
    return _run_cycle(
        SEARCH_MODULE,
        ["--once", "--only", search.slug],
        "search",
        target=f"{search.slug}:{search.product}",
        searches=[search],
    )


@shared_task(name="apps.fetching.tasks.repoll_searches")
def repoll_searches() -> int:
    with _cycle_lock("repoll_searches") as acquired:
        if not acquired:
            logger.warning("repoll_searches: skipped overlapping cycle")
            return 0
        reap_orphaned_fetch_runs()
        searches = list(Search.objects.filter(enabled=True))
        if not searches:
            return 0
        return _run_cycle(SEARCH_MODULE, ["--once"], "search", searches=searches)


@shared_task(name="apps.fetching.tasks.purge_expired_search_tweets")
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


@shared_task(name="apps.fetching.tasks.purge_old_fetch_runs")
def purge_old_fetch_runs() -> int:
    reap_orphaned_fetch_runs()
    cutoff = timezone.now() - timedelta(days=settings.FETCH_RUN_RETENTION_DAYS)
    deleted, _ = FetchRun.objects.filter(started_at__lt=cutoff).delete()
    return deleted
