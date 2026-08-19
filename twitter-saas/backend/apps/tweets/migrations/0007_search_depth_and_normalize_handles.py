from django.db import migrations, models


def normalize_handles(apps, schema_editor):
    TwitterUser = apps.get_model("tweets", "TwitterUser")
    for user in TwitterUser.objects.all().order_by("id"):
        normalized = user.handle.strip().lstrip("@").lower()
        if normalized == user.handle:
            continue
        if TwitterUser.objects.filter(handle=normalized).exclude(pk=user.pk).exists():
            print(
                f"normalize_handles: skipping TwitterUser id={user.pk} handle={user.handle!r}, "
                f"normalized form {normalized!r} already exists on another row"
            )
            continue
        user.handle = normalized
        user.save(update_fields=["handle"])


class Migration(migrations.Migration):
    dependencies = [("tweets", "0006_tweetmetric_and_search")]

    operations = [
        migrations.AddField(
            model_name="search",
            name="pagination_depth",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.RunPython(normalize_handles, migrations.RunPython.noop),
    ]
