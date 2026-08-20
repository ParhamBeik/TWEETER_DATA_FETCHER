from django.db import migrations


def enable_pg_trgm(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    # Indexed on lower(text) because NarrativesView's similarity() comparison is
    # case-insensitive (lower(first.text), lower(follower.text)); an index on the
    # raw column can't accelerate a query filtering on the lowercased expression.
    schema_editor.execute(
        "CREATE INDEX IF NOT EXISTS tweets_tweet_text_trgm "
        "ON tweets_tweet USING GIN (lower(text) gin_trgm_ops);"
    )


def disable_pg_trgm_index(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP INDEX IF EXISTS tweets_tweet_text_trgm;")


class Migration(migrations.Migration):
    dependencies = [("tweets", "0010_twitteruser_historical_backfilled_at")]

    operations = [migrations.RunPython(enable_pg_trgm, disable_pg_trgm_index)]
