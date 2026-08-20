from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tweets", "0003_extras_and_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="twitteruser",
            name="priority",
            field=models.PositiveSmallIntegerField(default=7),
        ),
    ]
