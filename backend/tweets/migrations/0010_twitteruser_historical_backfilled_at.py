from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tweets", "0009_search_rolling_hours"),
    ]

    operations = [
        migrations.AddField(
            model_name="twitteruser",
            name="historical_backfilled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
