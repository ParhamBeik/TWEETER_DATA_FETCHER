"""Celery app + periodic beat schedule."""
from __future__ import annotations

import os

from celery import Celery
from celery.schedules import schedule

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("twitter_saas")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# One queue per subsystem, each consumed by its own solo/concurrency=1 worker
# (see docker-compose.yml). Previously all three shared a single worker
# process, so a live cycle's in-process rate-limit sleep (which can run
# 10-15+ minutes) blocked historical and search entirely regardless of their
# own schedule -- separate queues/workers let them run concurrently instead.
app.conf.task_routes = {
    "fetching.tasks.poll_live_all": {"queue": "live"},
    "fetching.tasks.fetch_account_live": {"queue": "live"},
    "fetching.tasks.backfill_historical_all": {"queue": "historical"},
    "fetching.tasks.fetch_account_historical": {"queue": "historical"},
    "fetching.tasks.repoll_searches": {"queue": "search"},
    "fetching.tasks.run_search": {"queue": "search"},
    # The scheduler must NOT share the queue it schedules onto. It used to sit
    # on "search", behind the 5-15 minute fetches it had itself queued, so once
    # the backlog grew past the dispatch interval it stopped running entirely --
    # and it is the only thing that can re-queue a search, so every query not
    # already in the backlog froze for over a day. A scheduler starved by its
    # own work cannot recover on its own; giving it an uncontended queue is what
    # makes the cadence real rather than best-effort.
    "fetching.tasks.dispatch_due_searches": {"queue": "control"},
    # Cheap maintenance, no dedicated worker needed -- these share the control
    # queue for the same reason: they must not queue behind a long fetch.
    # Without an explicit route they would sit unconsumed forever, since no
    # worker listens to the old default queue.
    "fetching.tasks.purge_expired_search_tweets": {"queue": "control"},
    "fetching.tasks.purge_old_fetch_runs": {"queue": "control"},
    "fetching.tasks.purge_old_raw_pages": {"queue": "control"},
    "fetching.tasks.purge_old_tweet_metrics": {"queue": "control"},
    "fetching.tasks.recompute_poll_intervals": {"queue": "control"},
    "fetching.tasks.archive_media": {"queue": "control"},
}


@app.on_after_finalize.connect
def setup_periodic_tasks(sender, **_kwargs):
    from django.conf import settings

    sender.add_periodic_task(
        schedule(settings.FETCH_LIVE_INTERVAL_SECONDS),
        app.signature("fetching.tasks.poll_live_all"),
        name="live-poll-all-accounts",
    )
    sender.add_periodic_task(
        schedule(settings.FETCH_HISTORICAL_INTERVAL_SECONDS),
        app.signature("fetching.tasks.backfill_historical_all"),
        name="historical-backfill-all-accounts",
    )
    # Chunked photo downloads. 25 images, then yield the worker so dispatch
    # never waits on the ~2 GB backlog.
    sender.add_periodic_task(
        schedule(settings.MEDIA_ARCHIVE_INTERVAL_SECONDS),
        app.signature("fetching.tasks.archive_media"),
        name="archive-media",
    )
    # Cheap dispatcher rather than the fetch itself: it only checks which
    # searches are due and queues one task each, so per-search cadence lives in
    # the DB and every search gets a whole cycle budget instead of sharing one.
    sender.add_periodic_task(
        schedule(settings.FETCH_SEARCH_DISPATCH_SECONDS),
        app.signature("fetching.tasks.dispatch_due_searches"),
        name="dispatch-due-searches",
    )
    # Daily retention: search-only tweets (30d) and old FetchRun rows (90d).
    sender.add_periodic_task(
        schedule(86400.0),
        app.signature("fetching.tasks.purge_expired_search_tweets"),
        name="purge-expired-search-tweets",
    )
    sender.add_periodic_task(
        schedule(86400.0),
        app.signature("fetching.tasks.purge_old_fetch_runs"),
        name="purge-old-fetch-runs",
    )
    # Separate from the run purge: raw pages are 91% of the database and need a
    # much shorter clock than the run rows that reference them.
    sender.add_periodic_task(
        schedule(86400.0),
        app.signature("fetching.tasks.purge_old_raw_pages"),
        name="purge-old-raw-pages",
    )
    # Engagement snapshots, on the same daily cadence. Their cutoff matches the
    # 90-day ceiling analytics clamps every window to, so this can only delete
    # rows no chart could have shown.
    sender.add_periodic_task(
        schedule(86400.0),
        app.signature("fetching.tasks.purge_old_tweet_metrics"),
        name="purge-old-tweet-metrics",
    )
    # Daily re-tiering: polling cadence follows each account's measured posting
    # rate, so it has to be recomputed as that rate drifts.
    sender.add_periodic_task(
        schedule(86400.0),
        app.signature("fetching.tasks.recompute_poll_intervals"),
        name="recompute-poll-intervals",
    )
