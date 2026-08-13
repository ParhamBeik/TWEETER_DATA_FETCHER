from django.db import migrations, models


def _postgres_search_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "CREATE INDEX IF NOT EXISTS tweet_text_search_gin "
        "ON tweets_tweet USING GIN (to_tsvector('english', coalesce(text, '')));"
    )


def _drop_postgres_search_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP INDEX IF EXISTS tweet_text_search_gin;")


class Migration(migrations.Migration):

    dependencies = [
        ("tweets", "0005_remove_search_owner"),
    ]

    operations = [
        migrations.CreateModel(
            name="TweetMetric",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("likes", models.BigIntegerField(default=0)),
                ("retweets", models.BigIntegerField(default=0)),
                ("views", models.BigIntegerField(default=0)),
                ("captured_at", models.DateTimeField(auto_now_add=True)),
                (
                    "tweet",
                    models.ForeignKey(
                        on_delete=models.CASCADE,
                        related_name="metrics",
                        to="tweets.tweet",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="tweetmetric",
            index=models.Index(fields=["tweet", "-captured_at"], name="tweets_twee_tweet_i_7c1e4a_idx"),
        ),
        migrations.RunPython(_postgres_search_index, _drop_postgres_search_index),
    ]
