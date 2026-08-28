"""Split SearchTimeline hits out of the shared Tweet table.

Search results used to be Tweet rows joined through SearchResult, which put two
unrelated collectors in one table: the feed served a blend of both, and search
hits inherited the archive's retention instead of their own 30-day clock.

This migration is deliberately non-destructive. It copies every existing search
result into the new SearchTweet/SearchHit pair and leaves the original Tweet rows
alone; the search-only Tweet rows they duplicate are removed afterwards by
``manage.py purge_orphan_search_tweets``, which has a --dry-run and reports what
it would delete. A migration that silently deletes rows gives an operator no
chance to look first.
"""

import django.db.models.deletion
from django.db import migrations, models

# Every SearchTweet column that has a same-named Tweet column. source_subsystem
# has no counterpart: on SearchTweet it is always "search".
_COPIED_FIELDS = (
    "dedup_key", "tweet_id", "author_rest_id", "account", "author_id", "text",
    "url", "type", "created_at", "raw_created_at", "likes", "retweets",
    "replies", "quotes", "bookmarks", "views", "source_language",
    "source_endpoint", "conversation_id", "entities", "extras", "payload",
)

_BATCH = 2000


def copy_search_results(apps, schema_editor):
    SearchResult = apps.get_model("tweets", "SearchResult")
    SearchTweet = apps.get_model("tweets", "SearchTweet")
    SearchHit = apps.get_model("tweets", "SearchHit")

    # Chunked by primary key rather than sliced: the tweet payloads are JSONB and
    # a single unbounded pass over them holds one long transaction, which is the
    # same reason purge_old_raw_pages chunks its delete.
    last_id = 0
    while True:
        rows = list(
            SearchResult.objects.filter(id__gt=last_id)
            .select_related("tweet")
            .order_by("id")[:_BATCH]
        )
        if not rows:
            break
        last_id = rows[-1].id
        bodies = [
            SearchTweet(
                **{field: getattr(row.tweet, field) for field in _COPIED_FIELDS}
            )
            for row in rows
        ]
        SearchTweet.objects.bulk_create(bodies, ignore_conflicts=True)
        by_key = {
            body.dedup_key: body.id
            for body in SearchTweet.objects.filter(
                dedup_key__in=[body.dedup_key for body in bodies]
            ).only("id", "dedup_key")
        }
        SearchHit.objects.bulk_create(
            [
                SearchHit(
                    search_id=row.search_id,
                    search_tweet_id=by_key[row.tweet.dedup_key],
                    rank=row.rank,
                )
                for row in rows
                if row.tweet.dedup_key in by_key
            ],
            ignore_conflicts=True,
        )


def drop_copied_hits(apps, schema_editor):
    """Reverse: the SearchResult rows are still intact, so just empty the copy."""
    apps.get_model("tweets", "SearchHit").objects.all().delete()
    apps.get_model("tweets", "SearchTweet").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tweets', '0015_mediaasset'),
    ]

    operations = [
        migrations.AddField(
            model_name='fetchrun',
            name='search',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='runs', to='tweets.search'),
        ),
        migrations.AddField(
            model_name='search',
            name='queued_task_id',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.CreateModel(
            name='SearchTweet',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('dedup_key', models.CharField(max_length=160, unique=True)),
                ('tweet_id', models.CharField(db_index=True, max_length=64)),
                ('author_rest_id', models.CharField(blank=True, db_index=True, max_length=64, null=True)),
                ('account', models.CharField(db_index=True, max_length=100)),
                ('text', models.TextField(blank=True, default='')),
                ('url', models.URLField(blank=True, default='', max_length=500)),
                ('type', models.CharField(default='Tweet', max_length=20)),
                ('created_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('raw_created_at', models.CharField(blank=True, default='', max_length=64)),
                ('likes', models.BigIntegerField(default=0)),
                ('retweets', models.BigIntegerField(default=0)),
                ('replies', models.BigIntegerField(default=0)),
                ('quotes', models.BigIntegerField(default=0)),
                ('bookmarks', models.BigIntegerField(default=0)),
                ('views', models.BigIntegerField(default=0)),
                ('source_language', models.CharField(blank=True, max_length=16, null=True)),
                ('source_endpoint', models.CharField(blank=True, default='SearchTimeline', max_length=64)),
                ('conversation_id', models.CharField(blank=True, max_length=64, null=True)),
                ('entities', models.JSONField(blank=True, default=dict)),
                ('extras', models.JSONField(blank=True, default=dict)),
                ('payload', models.JSONField(blank=True, default=dict)),
                ('ingested_at', models.DateTimeField(auto_now_add=True)),
                ('author', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='search_tweets', to='tweets.twitteruser')),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='SearchHit',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('rank', models.IntegerField(default=0)),
                ('first_seen_at', models.DateTimeField(auto_now_add=True)),
                ('last_seen_at', models.DateTimeField(auto_now=True)),
                ('search', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='hits', to='tweets.search')),
                ('search_tweet', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='hits', to='tweets.searchtweet')),
            ],
            options={
                'ordering': ['rank', '-id'],
            },
        ),
        migrations.AddIndex(
            model_name='searchtweet',
            index=models.Index(fields=['-created_at', '-id'], name='tweets_sear_created_2cef73_idx'),
        ),
        migrations.AddIndex(
            model_name='searchtweet',
            index=models.Index(fields=['-ingested_at'], name='tweets_sear_ingeste_0fe199_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='searchhit',
            unique_together={('search', 'search_tweet')},
        ),
        # Ordered after the unique_together, not where makemigrations put the
        # DeleteModel: the copy relies on ON CONFLICT DO NOTHING against
        # (search, search_tweet), and SearchResult must still exist to read from.
        migrations.RunPython(copy_search_results, drop_copied_hits),
        migrations.DeleteModel(
            name='SearchResult',
        ),
    ]
