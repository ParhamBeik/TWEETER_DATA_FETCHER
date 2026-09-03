"""Seed PendingMedia from tweets that were ingested before the queue existed.

Ingest now records what needs downloading as it writes each tweet, so the
archiver reads a queue instead of full-scanning the tweet table every two
minutes looking for work. Rows written before that change have no queue entry,
and without this they would simply never be archived.

One-time, idempotent, and safe to re-run: remote_url is unique and conflicts are
ignored, so a second pass adds only what the first missed.

Reads only what is already in the database -- no refetch, no API budget spent.

    python manage.py seed_media_queue
    python manage.py seed_media_queue --dry-run
    python manage.py seed_media_queue --limit 5000
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from fetching.media import (
    VIDEO_BACKFILL_DAYS,
    _is_video_url,
    enqueue_media,
    is_allowed_photo_url,
    photo_urls_from_extras,
)
from tweets.models import MediaAsset, PendingMedia, Tweet

BATCH = 2000


class Command(BaseCommand):
    help = "Queue archivable media from tweets ingested before the queue existed."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Count without writing.")
        parser.add_argument(
            "--limit", type=int, default=0, help="Max tweets to scan (0 = all)."
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]

        # Video stays forward-only, exactly as the scanning archiver had it: the
        # back catalogue would be a large, unpredictable download for clips X has
        # mostly stopped serving anyway.
        video_cutoff = timezone.now() - timedelta(days=VIDEO_BACKFILL_DAYS)
        # Already on disk, so there is nothing to queue for it.
        archived = set(MediaAsset.objects.values_list("remote_url", flat=True))
        queued = set(PendingMedia.objects.values_list("remote_url", flat=True))

        queryset = Tweet.objects.exclude(extras={}).only("extras", "tweet_id", "ingested_at")
        if limit:
            queryset = queryset[:limit]

        scanned = 0
        pending: list[tuple[str, str]] = []
        added = 0
        for tweet in queryset.iterator(chunk_size=500):
            scanned += 1
            recent = tweet.ingested_at is not None and tweet.ingested_at >= video_cutoff
            for url in photo_urls_from_extras(tweet.extras):
                if url in archived or url in queued or not is_allowed_photo_url(url):
                    continue
                if _is_video_url(url) and not recent:
                    continue
                queued.add(url)
                pending.append((url, tweet.tweet_id))
            if len(pending) >= BATCH:
                added += self._flush(pending, dry_run)
                pending = []
        added += self._flush(pending, dry_run)

        verb = "would queue" if dry_run else "queued"
        self.stdout.write(
            self.style.SUCCESS(f"scanned {scanned} tweet(s), {verb} {added} media URL(s)")
        )

    def _flush(self, pending: list[tuple[str, str]], dry_run: bool) -> int:
        if not pending:
            return 0
        if dry_run:
            return len(pending)
        return enqueue_media(pending)
