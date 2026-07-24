"""Celery app + periodic beat schedule."""
from __future__ import annotations

import os

from celery import Celery
from celery.schedules import schedule

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("twitter_saas")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


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
