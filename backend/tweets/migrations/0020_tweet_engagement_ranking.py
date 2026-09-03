"""Make the feed's two ranked sorts indexable.

`sort=top` and `sort=views` are both offered over an unbounded window, so each
one scanned and sorted the whole tweet table. `top` had no index available at
all, because it ranked on an expression rather than a column.

DEPLOY COST: adding a *persisted* generated column rewrites the table. On a
large archive this holds an ACCESS EXCLUSIVE lock for the duration and needs
transient disk space for a full copy of the table -- plan a window for it, and
note that it runs after 0019, which frees the raw-page space first. That
ordering is deliberate; do not reorder these two.

The alternative was a functional index on the same expression, which needs no
rewrite. It was not chosen because the planner only uses one when the ORDER BY
matches the indexed expression exactly, and a stored column removes that
fragility permanently for a one-time cost.
"""

import django.db.models.expressions
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tweets', '0019_purge_raw_page_backlog'),
    ]

    operations = [
        migrations.AddField(
            model_name='tweet',
            name='engagement',
            field=models.GeneratedField(db_persist=True, expression=django.db.models.expressions.CombinedExpression(django.db.models.expressions.CombinedExpression(django.db.models.expressions.CombinedExpression(models.F('likes'), '+', models.F('retweets')), '+', models.F('replies')), '+', models.F('quotes')), output_field=models.BigIntegerField()),
        ),
        migrations.AddIndex(
            model_name='tweet',
            index=models.Index(fields=['-engagement', '-id'], name='tweets_engagement_rank_idx'),
        ),
        migrations.AddIndex(
            model_name='tweet',
            index=models.Index(fields=['-views', '-id'], name='tweets_views_rank_idx'),
        ),
    ]
