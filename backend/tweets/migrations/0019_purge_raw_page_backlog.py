"""Clear the accumulated RawPage backlog.

Raw GraphQL pages are ~91% of this database and grow ~750 MB/day, and nothing in
the application has ever read one back: the only reprocessing path
(`manage.py backfill_tweet_payloads`) replays `Tweet.payload` instead. Until now
they were also governed by two contradictory clocks -- an uncapped 7-day delete
inside every fetch run and a 30-day Celery task -- so the documented retention
setting was dead and the real one was undocumented.

The write path is being narrowed to runs worth debugging (see
`fetching.runner.RAW_PAGE_KEEP_STATUSES`), but that alone would leave the
existing backlog resident for a full retention window. This clears it in one
pass so the disk is reclaimed at deploy time.

Deliberately irreversible. Deleted pages cannot be reconstructed -- they are
snapshots of what X returned at a moment that has passed -- so `reverse` is a
no-op rather than a lie about being able to restore them. Take a dump first;
`scripts/backup_pg.sh` is what the deploy runbook uses.
"""
from __future__ import annotations

from django.db import migrations

# Same chunk size as fetching.tasks.purge_old_raw_pages, for the same reason: a
# single unbounded DELETE over millions of JSONB rows holds one long transaction
# and bloats WAL on a 1 GB container.
CHUNK = 5000


def purge_raw_pages(apps, schema_editor):
    RawPage = apps.get_model("tweets", "RawPage")
    total = 0
    while True:
        ids = list(RawPage.objects.values_list("pk", flat=True)[:CHUNK])
        if not ids:
            break
        deleted, _ = RawPage.objects.filter(pk__in=ids).delete()
        if not deleted:
            break
        total += deleted
    print(f"  purged {total} raw page(s)")


def noop_reverse(apps, schema_editor):
    """Nothing to restore. See the module docstring."""


class Migration(migrations.Migration):

    atomic = False

    dependencies = [("tweets", "0018_tweetmetric_tweets_metric_captured_idx")]

    operations = [migrations.RunPython(purge_raw_pages, noop_reverse, atomic=False)]
