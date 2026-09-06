"""Unit tests: engagement invariants and the era they apply to.

Pure arithmetic over one tweet's counts, so unit level covers every branch --
no database, no fixtures. The value of these tests is almost entirely in the
era boundary: get that wrong and the gate either cries wolf on 1,066 correct
historical rows or stays silent on the 3,218 broken ones.
"""
from datetime import datetime, timedelta, timezone

import pytest

from fetching.metric_gates import (
    VIEWS_ERA_START,
    check_metrics,
    source_views_of,
    summarize,
    violations_for,
)

PRE = datetime(2019, 5, 1, tzinfo=timezone.utc)
POST = datetime(2026, 5, 1, tzinfo=timezone.utc)


class _Row:
    def __init__(self, created_at, likes=0, retweets=0, replies=0, views=0,
                 type="Tweet", extras=None):
        self.created_at = created_at
        self.likes, self.retweets, self.replies, self.views = likes, retweets, replies, views
        self.type = type
        self.extras = extras if extras is not None else {}


def _repost(original_views, **counts):
    """A repost row shaped the way the ingest path stores one."""
    return _Row(
        POST, type="Retweet",
        extras={"retweeted_tweet": {"metrics": {"views": original_views}}},
        **counts,
    )


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


# Reposts --------------------------------------------------------------------
#
# Every flagged row in production was a repost -- 5,911 of 5,911, with no plain
# tweet, quote or reply among them. X returns the wrapper's own view count (how
# often *this* repost was rendered) beside the original's like and share counts,
# so the ratio was comparing two different posts.


def test_a_reposts_counts_are_judged_against_the_post_they_came_from():
    """The production shape: 8,937 likes on the original, 210 wrapper views."""
    assert check_metrics(
        created_at=POST, likes=8937, retweets=1328, replies=570,
        views=210, source_views=645503,
    ) == []


def test_an_original_that_really_was_over_liked_still_reports():
    assert check_metrics(created_at=POST, likes=50, views=210, source_views=10) == [
        "likes_exceed_views"
    ]


@pytest.mark.parametrize("source_views", [None, 0])
def test_without_a_captured_original_the_rows_own_views_still_apply(source_views):
    """Absent basis must not excuse the row, or a real parse fault goes quiet."""
    assert check_metrics(
        created_at=POST, likes=50, views=10, source_views=source_views
    ) == ["likes_exceed_views"]


def test_source_views_are_read_from_a_stored_repost():
    assert source_views_of(_repost(645503, likes=8937, views=210)) == 645503


@pytest.mark.parametrize(
    "extras",
    [{}, {"retweeted_tweet": None}, {"retweeted_tweet": {}},
     {"retweeted_tweet": {"metrics": None}}, {"retweeted_tweet": {"metrics": {"views": "n/a"}}}],
)
def test_a_repost_with_no_usable_original_degrades_instead_of_raising(extras):
    """One malformed payload must not abort an otherwise good ingest batch."""
    assert source_views_of(_Row(POST, type="Retweet", extras=extras)) == 0


def test_only_reposts_look_for_another_posts_views():
    assert source_views_of(
        _Row(POST, extras={"retweeted_tweet": {"metrics": {"views": 999}}})
    ) == 0


def test_the_batch_summary_and_the_row_check_agree_on_reposts():
    """Both call sites must resolve the same basis or the warning miscounts."""
    good_repost = _repost(645503, likes=8937, retweets=1328, replies=570, views=210)
    bad_plain = _Row(POST, likes=50, views=10)
    assert violations_for(good_repost) == []
    assert violations_for(bad_plain) == ["likes_exceed_views"]
    assert summarize([good_repost, bad_plain]) == {"likes_exceed_views": 1}
