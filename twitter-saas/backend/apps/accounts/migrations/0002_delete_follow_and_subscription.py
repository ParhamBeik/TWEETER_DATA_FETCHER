from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.DeleteModel(name="Follow"),
        migrations.DeleteModel(name="SearchSubscription"),
    ]
