"""The feed's two ranked sorts, and the bounds that make them affordable.

`sort=top` and `sort=views` are offered next to an "All time" window, so they
have to be answerable over the whole archive. Three things make that true: a
stored, indexed engagement column, an offset ceiling, and a paginator that does
not COUNT the archive to render one page. Each is asserted here, because each
one silently degrades rather than breaking.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from fetching.ingest import upsert_tweet
from tweets.analytics import ENGAGEMENT_FIELDS
from tweets.models import Tweet, TwitterUser


@pytest.fixture
def client(db):
    user = User.objects.create_user(username="reader", password="pw")
    api = APIClient()
    api.force_authenticate(user=user)
    return api


def _post(rest_id: str, *, likes=0, retweets=0, replies=0, quotes=0, views=0):
    tweet = upsert_tweet(
        {
            "rest_id": rest_id,
            "author_id": "1",
            "account": "jack",
            "text": f"post {rest_id}",
            "created_at": "Wed Oct 10 20:19:24 +0000 2018",
            "likes": likes,
            "retweets": retweets,
            "replies": replies,
            "quotes": quotes,
            "views": views,
        }
    )
    TwitterUser.objects.filter(handle="jack").update(tracking=True)
    return tweet


@pytest.mark.django_db
def test_engagement_column_sums_the_four_deliberate_actions():
    """The column is maintained by the database, so this is the only place the
    formula can be checked against what ingest actually stored.
    """
    tweet = _post("1", likes=3, retweets=5, replies=7, quotes=11, views=100_000)
    tweet.refresh_from_db()
    assert tweet.engagement == 26
    # Views are deliberately excluded: they run 100-1000x larger, so including
    # them would make "most engaged" a synonym for "most viewed".
    assert tweet.engagement != tweet.views


@pytest.mark.django_db
def test_engagement_definitions_agree():
    """Two definitions of one formula: the generated column on the model and
    ENGAGEMENT_FIELDS, which the raw-SQL analytics build their own version from.
    If these drift, the feed and the analytics disagree about what engagement is.
    """
    expression = Tweet._meta.get_field("engagement").expression
    referenced = {
        expr.name
        for expr in expression.flatten()
        if hasattr(expr, "name") and expr.name in {*ENGAGEMENT_FIELDS, "views"}
    }
    assert referenced == set(ENGAGEMENT_FIELDS)


@pytest.mark.django_db
def test_top_and_views_rank_independently(client):
    _post("low-eng-high-views", likes=1, views=9_000_000)
    _post("high-eng-low-views", likes=500, retweets=500, views=10)

    top = client.get("/api/feed/?sort=top").json()["results"]
    assert top[0]["tweet_id"] == "high-eng-low-views"

    views = client.get("/api/feed/?sort=views").json()["results"]
    assert views[0]["tweet_id"] == "low-eng-high-views"


@pytest.mark.django_db
def test_ranked_sorts_work_over_an_unbounded_window(client):
    """All time is a one-click option in the console, so it has to be served."""
    _post("a", likes=10)
    _post("b", likes=20)

    body = client.get("/api/feed/?sort=top&window=").json()
    assert [row["tweet_id"] for row in body["results"]] == ["b", "a"]


@pytest.mark.django_db
def test_ranked_pages_do_not_report_a_counted_total(client):
    """The COUNT(*) this paginator used to run scaled with the archive, not the
    page. `count` stays in the envelope for shape, explicitly null.
    """
    _post("a", likes=1)
    body = client.get("/api/feed/?sort=top").json()
    assert body["count"] is None
    assert set(body) == {"count", "next", "previous", "results"}


@pytest.mark.django_db
def test_next_link_appears_only_when_a_further_page_exists(client):
    for index in range(31):
        _post(str(index), likes=index)

    first = client.get("/api/feed/?sort=top&limit=30").json()
    assert len(first["results"]) == 30
    assert first["next"] is not None

    second = client.get("/api/feed/?sort=top&limit=30&offset=30").json()
    assert len(second["results"]) == 1
    # The one-row lookahead is what answers this without a count.
    assert second["next"] is None
    assert second["previous"] is not None


@pytest.mark.django_db
def test_deep_offset_is_refused_rather_than_served_slowly(client):
    _post("a", likes=1)
    response = client.get("/api/feed/?sort=top&offset=5000")
    assert response.status_code == 400
    assert "maximum offset" in response.json()["detail"]


@pytest.mark.django_db
def test_latest_sort_still_pages_by_cursor(client):
    """The offset ceiling must not reach the chronological feed, which is the
    default view and pages by cursor precisely so it has no such limit.
    """
    _post("a", likes=1)
    body = client.get("/api/feed/").json()
    assert "results" in body
    assert client.get("/api/feed/?offset=5000").status_code == 200


@pytest.mark.django_db
def test_next_link_suppressed_when_next_offset_would_exceed_max_offset(client):
    for index in range(10):
        _post(f"ceil-{index}", likes=index)
    response = client.get("/api/feed/?sort=top&offset=990&limit=20")
    assert response.status_code == 200
    assert response.json()["next"] is None
