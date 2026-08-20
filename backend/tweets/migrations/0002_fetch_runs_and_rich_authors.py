from django.db import migrations, models
import django.db.models.deletion


def backfill_authors(apps, schema_editor):
    Tweet = apps.get_model("tweets", "Tweet")
    TwitterUser = apps.get_model("tweets", "TwitterUser")
    for tweet in Tweet.objects.exclude(author=None).iterator():
        payload = tweet.payload if isinstance(tweet.payload, dict) else {}
        author = payload.get("author", {}) if isinstance(payload.get("author"), dict) else {}
        updates = {
            "rest_id": author.get("id") or payload.get("author_id") or tweet.author.rest_id,
            "display_name": author.get("display_name") or payload.get("author_display_name") or tweet.author.display_name,
            "avatar_url": author.get("avatar_url") or payload.get("author_avatar_url") or "",
            "verified": bool(author.get("verified") or payload.get("author_verified")),
            "verified_type": author.get("verified_type") or payload.get("author_verified_type") or "",
        }
        TwitterUser.objects.filter(pk=tweet.author_id).update(**updates)


class Migration(migrations.Migration):
    dependencies = [("tweets", "0001_initial")]

    operations = [
        migrations.AddField(model_name="twitteruser", name="avatar_url", field=models.URLField(blank=True, default="", max_length=500)),
        migrations.AddField(model_name="twitteruser", name="verified", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="twitteruser", name="verified_type", field=models.CharField(blank=True, default="", max_length=64)),
        migrations.AddField(model_name="twitteruser", name="quarantined", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="twitteruser", name="quarantine_reason", field=models.CharField(blank=True, default="", max_length=255)),
        migrations.AddField(model_name="twitteruser", name="quarantined_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.CreateModel(
            name="FetchRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("run_id", models.CharField(max_length=64, unique=True)),
                ("task_id", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("subsystem", models.CharField(db_index=True, max_length=32)),
                ("target", models.CharField(blank=True, db_index=True, default="", max_length=255)),
                ("status", models.CharField(choices=[("running", "Running"), ("completed", "Completed"), ("partial", "Partial"), ("failed", "Failed"), ("auth_required", "Auth required")], db_index=True, default="running", max_length=20)),
                ("return_code", models.IntegerField(blank=True, null=True)),
                ("summary", models.JSONField(blank=True, default=dict)),
                ("failure_ledger", models.JSONField(blank=True, default=dict)),
                ("log_excerpt", models.TextField(blank=True, default="")),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["-started_at"]},
        ),
        migrations.AddField(
            model_name="rawpage",
            name="fetch_run",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="raw_pages", to="tweets.fetchrun"),
        ),
        migrations.RunPython(backfill_authors, migrations.RunPython.noop),
    ]
