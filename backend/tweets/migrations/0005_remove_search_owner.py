from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("tweets", "0004_twitteruser_priority"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="search",
            unique_together=set(),
        ),
        migrations.RemoveField(
            model_name="search",
            name="owner",
        ),
        migrations.AlterUniqueTogether(
            name="search",
            unique_together={("slug", "product")},
        ),
    ]
