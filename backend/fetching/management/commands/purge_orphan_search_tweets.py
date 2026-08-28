"""Remove search-only rows left in the Tweet table by the domain split.

Migration 0016 copied every search result into SearchTweet/SearchHit but
deliberately deleted nothing: the originals are still sitting in `tweets_tweet`,
where they no longer belong -- the feed and the tracked-account analytics both
read that table, and a row that only ever arrived through a saved search now has
a home of its own with its own 30-day retention.

Separate from the migration because this half is irreversible. Run it with
--dry-run first and look at what it says.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from tweets.models import SearchTweet, Tweet, TwitterUser

_CHUNK = 5000


class Command(BaseCommand):
    help = "Delete Tweet rows that only ever arrived through a saved search."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted and change nothing.",
        )

    def handle(self, *args, **options):
        tracked = set(
            TwitterUser.objects.filter(tracking=True).values_list("handle", flat=True)
        )
        # Two conditions, both required. Having a copy in SearchTweet proves the
        # row came through a search; not being a tracked handle proves the
        # account collector has no claim on it. A tracked account's post that a
        # search also happened to match belongs to the archive and stays.
        #
        # A subquery rather than a Python set of dedup keys: there can be
        # millions, and shipping them all into the process to send them straight
        # back as an IN list is the expensive way to ask the same question.
        doomed = Tweet.objects.filter(
            dedup_key__in=SearchTweet.objects.values("dedup_key")
        ).exclude(account__in=tracked)

        total = doomed.count()
        by_account = (
            doomed.values("account").order_by().distinct().count() if total else 0
        )
        self.stdout.write(
            f"{total} search-only tweet(s) across {by_account} untracked account(s) "
            f"are duplicated in tweets_searchtweet."
        )
        if options["dry_run"]:
            self.stdout.write("Dry run: nothing deleted.")
            return
        if not total:
            return

        deleted = 0
        while True:
            ids = list(doomed.values_list("pk", flat=True)[:_CHUNK])
            if not ids:
                break
            # Chunked for the same reason purge_old_raw_pages is: one unbounded
            # DELETE over millions of JSONB rows bloats WAL on a small container.
            deleted += Tweet.objects.filter(pk__in=ids).delete()[0]
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} row(s)."))
