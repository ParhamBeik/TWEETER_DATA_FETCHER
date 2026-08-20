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
    "apps.fetching.tasks.poll_live_all": {"queue": "live"},
    "apps.fetching.tasks.fetch_account_live": {"queue": "live"},
    "apps.fetching.tasks.backfill_historical_all": {"queue": "historical"},
    "apps.fetching.tasks.fetch_account_historical": {"queue": "historical"},
    "apps.fetching.tasks.repoll_searches": {"queue": "search"},
    "apps.fetching.tasks.run_search": {"queue": "search"},
    # Cheap daily maintenance, no dedicated worker needed -- piggyback on
    # historical's queue. Without an explicit route these would sit
    # unconsumed forever now that no worker listens to the old default queue.
    "apps.fetching.tasks.purge_expired_search_tweets": {"queue": "historical"},
    "apps.fetching.tasks.purge_old_fetch_runs": {"queue": "historical"},
}


@app.on_after_finalize.connect
def setup_periodic_tasks(sender, **_kwargs):
    from django.conf import settings

    sender.add_periodic_task(
        schedule(settings.FETCH_LIVE_INTERVAL_SECONDS),
        app.signature("apps.fetching.tasks.poll_live_all"),
        name="live-poll-all-accounts",
    )
    sender.add_periodic_task(
        schedule(settings.FETCH_HISTORICAL_INTERVAL_SECONDS),
        app.signature("apps.fetching.tasks.backfill_historical_all"),
        name="historical-backfill-all-accounts",
    )
    sender.add_periodic_task(
        schedule(settings.FETCH_SEARCH_INTERVAL_SECONDS),
        app.signature("apps.fetching.tasks.repoll_searches"),
        name="repoll-enabled-searches",
    )
    # Daily retention: search-only tweets (30d) and old FetchRun rows (90d).
    sender.add_periodic_task(
        schedule(86400.0),
        app.signature("apps.fetching.tasks.purge_expired_search_tweets"),
        name="purge-expired-search-tweets",
    )
    sender.add_periodic_task(
        schedule(86400.0),
        app.signature("apps.fetching.tasks.purge_old_fetch_runs"),
        name="purge-old-fetch-runs",
    )
