"""Logical invariants on a tweet's engagement counts, and when they apply.

One shared definition of "these numbers cannot be real", used by ingest so a
parsing regression announces itself in the worker log instead of quietly
poisoning analytics months later.

The hard part is not the arithmetic, it is knowing when the arithmetic means
anything. X did not publish view counts before December 2022, so a 2019 tweet
with 400 likes and no views is *correct data*, not a parse failure -- there are
1,066 such rows in this corpus and every one of them is fine. Flagging them
would bury the 3,218 rows that are genuinely wrong. Every view-based invariant
below is therefore gated on the tweet being from the era where X reports views
at all.
"""
from __future__ import annotations

from datetime import datetime, timezone

# X began exposing per-tweet view counts publicly during December 2022. Rounded
# up to the following Jan 1 deliberately: the rollout was staged over weeks, so
# late-December tweets are genuinely ambiguous and a boundary inside the rollout
# would flag correct data. Before this date "views == 0" carries no information
# and cannot contradict anything.
VIEWS_ERA_START = datetime(2023, 1, 1, tzinfo=timezone.utc)

# Engagement that necessarily implies a view: you cannot like, repost or reply
# to something you did not see. Bookmarks and quotes are deliberately excluded
# only because they are rarer, not because they differ in principle.
_IMPLIES_A_VIEW = ("likes", "retweets", "replies")


def _in_views_era(created_at) -> bool:
    if created_at is None:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at >= VIEWS_ERA_START


def check_metrics(
    *, created_at, likes=0, retweets=0, replies=0, views=0, source_views=None
) -> list[str]:
    """Names of the invariants this tweet violates. Empty means plausible.

    Returns names rather than raising: one bad row is a data-quality signal to
    count, not a reason to abort a batch that is otherwise fine.

    `source_views` is the view count of the post these engagement figures
    actually describe, when that is a different post from the row itself. Only a
    repost has one: X hands back the wrapper's own view count (a few hundred --
    the times *this* repost was rendered) beside the original's like, reply and
    repost counts (tens of thousands). Comparing those two numbers is a category
    error rather than a data fault, and it accounted for every single flagged
    row in production -- 5,911 of 5,911, with not one plain tweet, quote or
    reply among them. Given the original's views the ratio compares one post
    against itself again and means something.

    Strict keywords, no **kwargs catch-all: both callers pass these by name, and
    swallowing a typo would silently read the field as 0 and then *invent* an
    `engagement_without_views` violation out of it.
    """
    counts = {"likes": int(likes or 0), "retweets": int(retweets or 0), "replies": int(replies or 0)}
    # The denominator belongs to whichever post the counts came from. Falling
    # back to the row's own views keeps every non-repost on the original path.
    views = int(source_views or 0) or int(views or 0)
    if not _in_views_era(created_at):
        return []

    violations = []
    if views == 0 and any(counts[field] for field in _IMPLIES_A_VIEW):
        violations.append("engagement_without_views")
    if views:
        # Every reaction is one viewer at minimum, so no reaction count can
        # exceed the view count. This is what catches a repost row that kept the
        # original's share count but lost its view count.
        for field in _IMPLIES_A_VIEW:
            if counts[field] > views:
                violations.append(f"{field}_exceed_views")
    return violations


def source_views_of(row) -> int:
    """Views of the post whose engagement figures `row` carries, or 0.

    Non-zero only for a repost that captured its original, which the ingest
    path already stores at `extras.retweeted_tweet.metrics.views` -- every one
    of the 5,911 mismatched rows in production had it. Duck-typed on purpose so
    this module stays importable without Django.
    """
    if getattr(row, "type", None) != "Retweet":
        return 0
    extras = getattr(row, "extras", None)
    original = extras.get("retweeted_tweet") if isinstance(extras, dict) else None
    metrics = original.get("metrics") if isinstance(original, dict) else None
    if not isinstance(metrics, dict):
        return 0
    try:
        return int(metrics.get("views") or 0)
    except (TypeError, ValueError):
        return 0


def violations_for(row) -> list[str]:
    """`check_metrics` for a stored row, resolving its own comparison basis.

    One function so the batch summary and the offending-row count cannot drift
    apart: they used to build the same keyword set at two call sites, and a
    denominator added to one of them would have quietly skipped the other.
    """
    return check_metrics(
        created_at=row.created_at,
        likes=row.likes,
        retweets=row.retweets,
        replies=row.replies,
        views=row.views,
        source_views=source_views_of(row),
    )


def summarize(rows) -> dict[str, int]:
    """Violation counts across an ingest batch, for the ingest warning line.

    Counts violations, not rows -- one row can trip several. Callers that want
    a row count must count offenders themselves.
    """
    totals: dict[str, int] = {}
    for row in rows:
        for name in violations_for(row):
            totals[name] = totals.get(name, 0) + 1
    return totals


if __name__ == "__main__":  # runnable self-check
    from datetime import timedelta
    from types import SimpleNamespace

    pre = datetime(2019, 5, 1, tzinfo=timezone.utc)
    post = datetime(2026, 5, 1, tzinfo=timezone.utc)

    # The false alarm this module exists to avoid.
    assert check_metrics(created_at=pre, likes=400, views=0) == []
    # The real bug: a repost carrying the original's share count and no views.
    assert check_metrics(created_at=post, retweets=1200, views=0) == ["engagement_without_views"]
    # Impossible ratio inside the views era.
    assert check_metrics(created_at=post, likes=50, views=10) == ["likes_exceed_views"]
    # A plausible post is silent.
    assert check_metrics(created_at=post, likes=50, retweets=3, replies=1, views=9000) == []
    # No timestamp means no era, so no view-based claim can be made.
    assert check_metrics(created_at=None, likes=5, views=0) == []
    # Naive datetimes are treated as UTC rather than crashing.
    assert check_metrics(created_at=post.replace(tzinfo=None), likes=1, views=0) == [
        "engagement_without_views"
    ]
    # Boundary: the first instant of the views era is in it.
    assert check_metrics(created_at=VIEWS_ERA_START, likes=1, views=0) == [
        "engagement_without_views"
    ]
    assert check_metrics(created_at=VIEWS_ERA_START - timedelta(seconds=1), likes=1, views=0) == []

    # A repost judged against the original's views, which is the post its like
    # count came from. The production shape: 8,937 likes, 210 wrapper views.
    assert check_metrics(
        created_at=post, likes=8937, retweets=1328, replies=570, views=210, source_views=645503
    ) == []
    # A repost whose original really was over-liked still reports.
    assert check_metrics(created_at=post, likes=50, views=210, source_views=10) == [
        "likes_exceed_views"
    ]
    # No original captured: fall back to the row's own views rather than
    # excusing the row, so a genuine parse fault is still visible.
    assert check_metrics(created_at=post, likes=50, views=10, source_views=0) == [
        "likes_exceed_views"
    ]

    repost = SimpleNamespace(
        type="Retweet", created_at=post, likes=8937, retweets=1328, replies=570, views=210,
        extras={"retweeted_tweet": {"metrics": {"views": 645503}}},
    )
    plain = SimpleNamespace(
        type="Tweet", created_at=post, likes=50, retweets=0, replies=0, views=10, extras={},
    )
    assert source_views_of(repost) == 645503
    assert source_views_of(plain) == 0
    assert violations_for(repost) == []
    assert violations_for(plain) == ["likes_exceed_views"]
    assert summarize([repost, plain]) == {"likes_exceed_views": 1}
    # A repost with no captured original, and a malformed one, both degrade to
    # the row's own views instead of raising mid-batch.
    assert source_views_of(SimpleNamespace(type="Retweet", extras={})) == 0
    assert source_views_of(SimpleNamespace(type="Retweet", extras={"retweeted_tweet": None})) == 0
    print("metric_gates self-check OK")
