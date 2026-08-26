"""Unit tests: engagement invariants and the era they apply to.

Pure arithmetic over one tweet's counts, so unit level covers every branch --
no database, no fixtures. The value of these tests is almost entirely in the
era boundary: get that wrong and the gate either cries wolf on 1,066 correct
historical rows or stays silent on the 3,218 broken ones.
"""
from datetime import datetime, timedelta, timezone

import pytest

from fetching.metric_gates import VIEWS_ERA_START, check_metrics, summarize

PRE = datetime(2019, 5, 1, tzinfo=timezone.utc)
POST = datetime(2026, 5, 1, tzinfo=timezone.utc)


class _Row:
    def __init__(self, created_at, likes=0, retweets=0, replies=0, views=0):
        self.created_at = created_at
        self.likes, self.retweets, self.replies, self.views = likes, retweets, replies, views


@pytest.mark.parametrize("field", ["likes", "retweets", "replies"])
def test_engagement_without_views_is_impossible_in_the_views_era(field):
    assert check_metrics(created_at=POST, views=0, **{field: 500}) == [
        "engagement_without_views"
    ]


@pytest.mark.parametrize("field", ["likes", "retweets", "replies"])
def test_the_same_shape_is_correct_data_before_x_published_views(field):
    """X had no public view counts until Dec 2022; zero there means 'unknown'."""
    assert check_metrics(created_at=PRE, views=0, **{field: 500}) == []


def test_the_era_boundary_is_inclusive():
    assert check_metrics(created_at=VIEWS_ERA_START, likes=1, views=0)
    assert not check_metrics(
        created_at=VIEWS_ERA_START - timedelta(seconds=1), likes=1, views=0
    )


@pytest.mark.parametrize(
    "field, expected",
    [("likes", "likes_exceed_views"), ("retweets", "retweets_exceed_views"),
     ("replies", "replies_exceed_views")],
)
def test_no_reaction_can_outnumber_the_views_that_produced_it(field, expected):
    assert check_metrics(created_at=POST, views=10, **{field: 50}) == [expected]


def test_a_plausible_post_is_silent():
    assert check_metrics(
        created_at=POST, likes=50, retweets=3, replies=1, views=9000
    ) == []


def test_a_tweet_with_no_timestamp_makes_no_era_claim():
    """15 rows have a null created_at; guessing an era for them invents facts."""
    assert check_metrics(created_at=None, likes=5, views=0) == []


def test_a_naive_timestamp_is_read_as_utc_rather_than_crashing():
    assert check_metrics(created_at=POST.replace(tzinfo=None), likes=1, views=0) == [
        "engagement_without_views"
    ]


def test_zero_engagement_is_never_flagged():
    """A genuinely unseen post is ordinary, not a parse failure."""
    assert check_metrics(created_at=POST, views=0) == []


def test_summarize_counts_each_violation_across_a_batch():
    totals = summarize([
        _Row(POST, retweets=1200),          # engagement_without_views
        _Row(POST, likes=4),                # engagement_without_views
        _Row(POST, likes=50, views=10),     # likes_exceed_views
        _Row(PRE, likes=999),               # correct historical data
        _Row(POST, likes=2, views=800),     # fine
    ])
    assert totals == {"engagement_without_views": 2, "likes_exceed_views": 1}
