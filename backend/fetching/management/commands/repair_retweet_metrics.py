"""Backfill repost rows whose engagement was taken from the wrapper.

Reposts were stored with the retweet wrapper's counts, which carry the
original's likes and shares but no view count of its own. The correct figure was
already captured in extras.retweeted_tweet.metrics; it just never reached the
columns. fetching.ingest.metrics_for fixes this going forward -- this repairs
the rows written before it.

Reads only what is already in the database: no refetch, no API budget spent.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from fetching.ingest import METRIC_FIELDS, _count
from fetching.metric_gates import check_metrics
from tweets.models import Tweet

BATCH = 1000


class Command(BaseCommand):
    help = "Recover repost engagement counts from the stored original."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        qs = Tweet.objects.filter(type="Retweet").only(
            "id", "created_at", "extras", *METRIC_FIELDS
        )

        scanned = repaired = 0
        still_bad = 0
        pending: list[Tweet] = []

        for tweet in qs.iterator(chunk_size=BATCH):
            scanned += 1
            source = ((tweet.extras or {}).get("retweeted_tweet") or {}).get("metrics")
            if not isinstance(source, dict):
                continue
            changed = False
            for field in METRIC_FIELDS:
                # Only ever fill a gap: a row that already has a figure keeps
                # it, so re-running this is a no-op rather than a slow drift.
                recovered = _count(source.get(field))
                if not getattr(tweet, field) and recovered:
                    setattr(tweet, field, recovered)
                    changed = True
            if not changed:
                continue
            repaired += 1
            if check_metrics(
                created_at=tweet.created_at,
                likes=tweet.likes,
                retweets=tweet.retweets,
                replies=tweet.replies,
                views=tweet.views,
            ):
                still_bad += 1
            # Not accumulated in dry-run: the list would never be drained, so a
            # scan over millions of reposts would hold every Tweet (each with
            # its JSONB extras) in memory -- and dry-run is the first thing an
            # operator runs, on a container with a few hundred MB.
            if dry_run:
                continue
            pending.append(tweet)
            if len(pending) >= BATCH:
                self._flush(pending)

        if pending:
            self._flush(pending)

        verb = "would repair" if dry_run else "repaired"
        self.stdout.write(
            f"scanned {scanned} repost(s), {verb} {repaired}; "
            f"{still_bad} still fail a sanity check after repair"
        )

    @staticmethod
    def _flush(pending: list[Tweet]) -> None:
        with transaction.atomic():
            Tweet.objects.bulk_update(pending, list(METRIC_FIELDS))
        pending.clear()
