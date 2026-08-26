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


def check_metrics(*, created_at, likes=0, retweets=0, replies=0, views=0) -> list[str]:
    """Names of the invariants this tweet violates. Empty means plausible.

    Returns names rather than raising: one bad row is a data-quality signal to
    count, not a reason to abort a batch that is otherwise fine.

    Strict keywords, no **kwargs catch-all: both callers pass these by name, and
    swallowing a typo would silently read the field as 0 and then *invent* an
    `engagement_without_views` violation out of it.
    """
    counts = {"likes": int(likes or 0), "retweets": int(retweets or 0), "replies": int(replies or 0)}
    views = int(views or 0)
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


def summarize(rows) -> dict[str, int]:
    """Violation counts across an ingest batch, for the ingest warning line.

    Counts violations, not rows -- one row can trip several. Callers that want
    a row count must count offenders themselves.
    """
    totals: dict[str, int] = {}
    for row in rows:
        for name in check_metrics(
            created_at=row.created_at,
            likes=row.likes,
            retweets=row.retweets,
            replies=row.replies,
            views=row.views,
        ):
            totals[name] = totals.get(name, 0) + 1
    return totals


if __name__ == "__main__":  # runnable self-check
    from datetime import timedelta

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
    print("metric_gates self-check OK")
