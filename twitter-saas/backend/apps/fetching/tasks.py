"""Celery tasks: run the vendored fetcher and ingest into Postgres."""
from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.tweets.models import Search, TwitterUser

from . import runner
from .ingest import ingest_search_results, ingest_tweets

logger = logging.getLogger(__name__)

HISTORICAL_MODULE = "tweeter_data_fetcher.pipelines.historical.service"
LIVE_MODULE = "tweeter_data_fetcher.pipelines.live.service"
SEARCH_MODULE = "tweeter_data_fetcher.pipelines.search.service"


def _run_and_ingest(module: str, args: list[str], subsystem: str) -> int:
    root = runner.run_fetcher(module, args, subsystem)
    try:
        return ingest_tweets(runner.iter_processed_tweets(root, subsystem))
    finally:
        runner.cleanup(root)


@shared_task(name="apps.fetching.tasks.fetch_account_historical")
def fetch_account_historical(handle: str) -> int:
    # The historical service runs a single pass by design; it has no --once flag
    # (only --only selects accounts), unlike the live/search loops.
    return _run_and_ingest(HISTORICAL_MODULE, ["--only", handle], "historical")


@shared_task(name="apps.fetching.tasks.fetch_account_live")
def fetch_account_live(handle: str) -> int:
    return _run_and_ingest(LIVE_MODULE, ["--once", "--account", handle], "live")


def _tracked_handles() -> list[str]:
    cap = settings.FETCH_MAX_ACCOUNTS_PER_RUN
    return list(
        TwitterUser.objects.filter(tracking=True)
        .order_by("id")
        .values_list("handle", flat=True)[:cap]
    )


@shared_task(name="apps.fetching.tasks.poll_live_all")
def poll_live_all() -> int:
    total = 0
    for handle in _tracked_handles():
        try:
            total += fetch_account_live(handle)
        except Exception:
            logger.exception("poll_live_all: live fetch failed for handle=%s", handle)
    return total


@shared_task(name="apps.fetching.tasks.backfill_historical_all")
def backfill_historical_all() -> int:
    total = 0
    for handle in _tracked_handles():
        try:
            total += fetch_account_historical(handle)
        except Exception:
            logger.exception(
                "backfill_historical_all: historical fetch failed for handle=%s", handle
            )
    return total


@shared_task(name="apps.fetching.tasks.run_search")
def run_search(search_id: int) -> int:
    search = Search.objects.filter(id=search_id).first()
    if search is None:
        return 0
    root = runner.run_fetcher(
        SEARCH_MODULE, ["--once", "--only", search.slug], "search", searches=[search]
    )
    try:
        count = ingest_search_results(search, runner.iter_search_tweets(root, search.slug))
    finally:
        runner.cleanup(root)
    search.last_run_at = timezone.now()
    search.save(update_fields=["last_run_at"])
    return count


@shared_task(name="apps.fetching.tasks.repoll_searches")
def repoll_searches() -> int:
    total = 0
    for search_id in Search.objects.filter(enabled=True).values_list("id", flat=True):
        try:
            total += run_search(search_id)
        except Exception:
            logger.exception("repoll_searches: search failed for id=%s", search_id)
    return total
