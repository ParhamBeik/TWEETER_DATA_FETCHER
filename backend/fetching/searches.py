"""One saved search as a unit of work: its schedule, its state, its teardown.

A search is not just a row -- it owns a recurring job, a run history, a
pagination cursor, a pile of raw GraphQL pages and a set of results. Every one of
those had to be reasoned about separately before this module existed, which is
why nothing in the console could say when a query would next run and why deleting
one was not offered at all.

Mirrors `fetching.accounts`: read helpers the API renders, and one write path per
operation, shared by the API, the admin and any command.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from tweets.models import EndpointState, FetchRun, RawPage, Search, SearchTweet

from .runner import normalize_slug

logger = logging.getLogger(__name__)

SEARCH_ENDPOINT = "SearchTimeline"

# RawPage rows are JSONB and a search that has run for weeks owns thousands of
# them; delete in chunks for the same reason purge_old_raw_pages does.
_RAW_PAGE_CHUNK = 5000


def raw_page_key(search: Search) -> str:
    """RawPage.account for this search's pages.

    The engine writes them under ``data/search/raw/<slug>/<product>/`` and
    `runner._persist_raw_pages` joins those two path segments with a single
    colon. Teardown has to reproduce that byte for byte or it deletes nothing.
    """
    return f"{normalize_slug(search.slug)}:{search.product.lower()}"


def endpoint_state_key(search: Search) -> str:
    """EndpointState.account for this search's pagination state.

    Deliberately *not* the same string as `raw_page_key`: the engine's
    `_state_key` joins slug and product with a double colon
    (``fetcher.search.SearchTimelineMonitor._state_key``) while the raw-page path
    join produces a single one. Two spellings for the same search is the kind of
    thing that makes a delete look successful and leave a live cursor behind, so
    both live here, next to each other.
    """
    return f"{normalize_slug(search.slug)}::{search.product.lower()}"


def next_due_at(search: Search):
    """When the dispatcher will next consider this search, or None if never run.

    A search that has never run is due immediately -- that is what makes creating
    one feel instant rather than "sometime in the next half hour".
    """
    if search.last_run_at is None:
        return None
    return search.last_run_at + timedelta(seconds=search.interval_seconds)


def schedule_for(search: Search, *, running: bool | None = None) -> dict:
    """The recurring job behind one search, as the console renders it.

    `state` is the single word the UI puts on a status dot:

    - ``paused``  -- disabled, the dispatcher skips it entirely
    - ``running`` -- a fetch is in flight right now
    - ``queued``  -- claimed by the dispatcher, waiting on the search worker
    - ``idle``    -- neither; `seconds_until_due` says how long that lasts

    `running` is accepted pre-computed so a list view can resolve every row from
    one query instead of one per search.
    """
    now = timezone.now()
    due_at = next_due_at(search)
    if running is None:
        running = FetchRun.objects.filter(search=search, status="running").exists()
    if not search.enabled:
        state = "paused"
    elif running:
        state = "running"
    elif search.queued_task_id:
        state = "queued"
    else:
        state = "idle"
    return {
        "state": state,
        "enabled": search.enabled,
        "interval_seconds": search.interval_seconds,
        "last_run_at": search.last_run_at,
        "next_due_at": due_at,
        "seconds_until_due": (
            max(0, int((due_at - now).total_seconds())) if due_at else 0
        ),
        # Never run, or overdue. The dispatcher queues these on its next tick.
        "is_due": search.enabled and (due_at is None or due_at <= now),
        "queued_task_id": search.queued_task_id,
    }


def revoke_queued_run(search: Search) -> bool:
    """Cancel a queued-but-not-started fetch for this search.

    Best effort by design: the id may already have been consumed, and the broker
    may be unreachable while the operator is deleting things. A failure here must
    not block the delete -- worst case the task starts, finds no Search row, and
    returns 0 (see fetching.tasks.run_search).
    """
    task_id = search.queued_task_id
    if not task_id:
        return False
    try:
        from config.celery import app

        app.control.revoke(task_id)
    except Exception:  # pragma: no cover - broker state, not logic
        logger.warning("revoke_queued_run(%s): could not revoke %s", search.slug, task_id)
        return False
    return True


def teardown_search(search: Search) -> dict[str, int]:
    """Delete a search and everything the pipeline built behind it.

    The whole point of the operation: removing a phrase has to remove its
    schedule, its queued work, its results, its run history and its pagination
    state, or the next search that happens to reuse the slug resumes from a
    stranded cursor and the console shows runs for a query nobody can see.

    The shared request-state blobs (rate limits, endpoint health) are left alone
    on purpose -- they are one row for the whole search subsystem, not per query.

    Returns what was removed so the caller can report it.
    """
    revoked = revoke_queued_run(search)
    label = endpoint_state_key(search)
    counts: dict[str, int] = {"revoked_queued_run": int(revoked)}

    with transaction.atomic():
        # Hits go with the search by cascade; the bodies only go if no other
        # saved search still points at them.
        tweet_ids = list(search.hits.values_list("search_tweet_id", flat=True))
        counts["hits"] = len(tweet_ids)
        search.hits.all().delete()
        counts["search_tweets"] = SearchTweet.objects.filter(
            id__in=tweet_ids, hits__isnull=True
        ).delete()[0]

        counts["endpoint_state"] = EndpointState.objects.filter(
            account=label, endpoint=SEARCH_ENDPOINT
        ).delete()[0]
        counts["fetch_runs"] = FetchRun.objects.filter(search=search).delete()[0]
        raw_key = raw_page_key(search)
        search.delete()

    # Outside the transaction and chunked: this is the one unbounded set here,
    # and holding a lock on thousands of JSONB rows to finish a UI action is how
    # a delete button becomes a database incident.
    counts["raw_pages"] = _delete_raw_pages(raw_key)
    logger.info("teardown_search(%s): %s", label, counts)
    return counts


def _delete_raw_pages(account: str) -> int:
    total = 0
    while True:
        ids = list(
            RawPage.objects.filter(
                endpoint=SEARCH_ENDPOINT, account=account
            ).values_list("pk", flat=True)[:_RAW_PAGE_CHUNK]
        )
        if not ids:
            return total
        total += RawPage.objects.filter(pk__in=ids).delete()[0]
