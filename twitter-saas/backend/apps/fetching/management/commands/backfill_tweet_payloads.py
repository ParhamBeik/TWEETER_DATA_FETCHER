"""Backfill author / rich columns from stored Tweet.payload without new X requests.

Older rows may lack TwitterUser avatar/verified fields or nested payload keys that
the current ingest path already understands. Re-running upsert_tweet on each
payload refreshes columns and author rows in place.

Usage:
    python manage.py backfill_tweet_payloads
    python manage.py backfill_tweet_payloads --limit 500
    python manage.py backfill_tweet_payloads --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.fetching.ingest import upsert_tweet
from apps.tweets.models import Tweet


class Command(BaseCommand):
    help = "Re-ingest Tweet.payload into author + rich tweet columns (no X traffic)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Max rows to process (0 = all).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count eligible rows without writing.",
        )
        parser.add_argument(
            "--only-sparse",
            action="store_true",
            help="Only rows missing author FK or empty author display_name.",
        )

    def handle(self, *args, **options) -> None:
        qs = Tweet.objects.exclude(payload={}).order_by("id")
        if options["only_sparse"]:
            qs = qs.filter(Q(author__isnull=True) | Q(author__display_name=""))
        limit = int(options["limit"] or 0)

        if options["dry_run"]:
            total = qs.count() if limit <= 0 else qs[:limit].count()
            self.stdout.write(self.style.WARNING(f"dry-run: {total} eligible tweet(s)"))
            return

        if limit > 0:
            qs = qs[:limit]

        updated = 0
        skipped = 0
        for tweet in qs.iterator(chunk_size=200):
            payload = tweet.payload if isinstance(tweet.payload, dict) else {}
            if not payload:
                skipped += 1
                continue
            item = dict(payload)
            item.setdefault("rest_id", tweet.tweet_id)
            item.setdefault("tweet_id", tweet.tweet_id)
            item.setdefault("account", tweet.account)
            if tweet.author_rest_id:
                item.setdefault("author_id", tweet.author_rest_id)
            if upsert_tweet(item) is not None:
                updated += 1
            else:
                skipped += 1

        self.stdout.write(
            self.style.SUCCESS(f"Backfilled {updated} tweet(s); skipped {skipped}.")
        )
