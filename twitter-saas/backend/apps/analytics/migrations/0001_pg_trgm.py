from django.db import migrations


def enable_pg_trgm(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    schema_editor.execute(
        "CREATE INDEX IF NOT EXISTS tweets_tweet_text_trgm "
        "ON tweets_tweet USING GIN (text gin_trgm_ops);"
    )


def disable_pg_trgm_index(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP INDEX IF EXISTS tweets_tweet_text_trgm;")


class Migration(migrations.Migration):
    dependencies = [("tweets", "0007_search_depth_and_normalize_handles")]

    operations = [migrations.RunPython(enable_pg_trgm, disable_pg_trgm_index)]
