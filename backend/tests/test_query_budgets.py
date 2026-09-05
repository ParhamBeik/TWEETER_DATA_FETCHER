"""Query-count regressions for the list endpoints the console polls.

Every assertion here is of one shape: serving N rows must not cost N queries.
These are the failures that never show up in a functional test -- the response
body is identical either way -- and that only become visible in production once
the roster or the archive is large enough for the difference to hurt. The feed
and the budget rail are re-polled every 20-30 seconds per open tab, so a
reintroduced N+1 here is a continuous load, not a slow page.

The numbers are deliberately upper bounds rather than exact counts: the point is
that the cost does not scale with the number of rows, not that it never moves.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from django.db import connection
from django.test.utils import CaptureQueriesContext

from fetching.ingest import upsert_tweet
from tweets.models import (
    EndpointState,
    ExportJob,
    FetchRun,
    MediaAsset,
    Search,
    SearchHit,
    SearchTweet,
    Tweet,
    TwitterUser,
)


@pytest.fixture
def staff_client(db):
    user = User.objects.create_user(username="ops", password="pw", is_staff=True)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _media_tweet(index: int, media_root) -> None:
    """A tweet with one archived photo, so the media rewrite path is exercised."""
    url = f"https://pbs.twimg.com/media/photo{index}.jpg"
    relative = f"ab/photo{index}.jpg"
    path = media_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"jpeg")
    MediaAsset.objects.create(remote_url=url, relative_path=relative)
    upsert_tweet(
        {
            "rest_id": str(9000 + index),
            "author_id": str(index),
            "account": f"acct{index}",
            "text": f"post {index}",
            "created_at": "Wed Oct 10 20:19:24 +0000 2018",
            "media": [{"type": "photo", "url": url}],
        }
    )


@pytest.mark.django_db
def test_feed_media_lookup_does_not_scale_with_page_size(
    staff_client, settings, tmp_path, django_assert_max_num_queries
):
    """One MediaAsset lookup for the page, not one per post.

    The avatar lookup on the author serializer was already batched; the photo
    lookup beside it was not, so every tweet on the feed cost its own query.
    """
    settings.MEDIA_ROOT = tmp_path
    for index in range(8):
        _media_tweet(index, tmp_path)
        TwitterUser.objects.filter(handle=f"acct{index}").update(tracking=True)

    # Measured flat at 4 for 1, 8 and 30 posts; before batching it was 4 + one
    # MediaAsset query per post, i.e. 34 on a full page.
    with django_assert_max_num_queries(6):
        response = staff_client.get("/api/feed/?include_untracked=1")
    assert response.status_code == 200
    assert len(response.json()["results"]) == 8
    # The archived copy is what the console is told to load, not X's CDN.
    served = response.json()["results"][0]["media"][0]["url"]
    assert served.startswith("/media/"), served


@pytest.mark.django_db
def test_account_roster_cost_is_flat_in_account_count(
    staff_client, django_assert_max_num_queries
):
    """The roster is unpaginated, so a per-account query is a per-roster query.

    Watermarks and the 24h post count were both resolved per row, which made a
    64-account roster ~130 queries for a screen the console loads on every visit.
    """
    for index in range(10):
        handle = f"tracked{index}"
        TwitterUser.objects.create(handle=handle, tracking=True, priority=3)
        EndpointState.objects.create(
            account=handle, endpoint="UserTweets", data={"fetch_watermark": "2026-01-01"}
        )

    # Measured flat at 6 for 1, 10 and 40 accounts; before batching it was
    # 6 + two queries per account, i.e. 86 for a 40-account roster.
    with django_assert_max_num_queries(8):
        response = staff_client.get("/api/accounts/")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 10
    # Batching must not lose the per-account values it replaced.
    assert body[0]["watermarks"]["UserTweets"] == "2026-01-01"
    assert body[0]["recent_tweet_count"] == 0


@pytest.mark.django_db
def test_saved_search_list_loads_last_runs_in_one_query(
    staff_client, django_assert_max_num_queries
):
    """`last_run` was the only field on this list still costing a query per row."""
    for index in range(6):
        search = Search.objects.create(
            name=f"query {index}", slug=f"query-{index}", raw_query=f"term{index}"
        )
        for attempt in range(3):
            FetchRun.objects.create(
                run_id=f"run-{index}-{attempt}",
                subsystem="search",
                search=search,
                status="completed",
                summary={"ingested_tweets": attempt, "new_tweets": attempt},
            )

    # Measured flat at 2 for 1, 6 and 20 searches.
    with django_assert_max_num_queries(4):
        response = staff_client.get("/api/searches/")
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 6
    # Newest run per search, not an arbitrary one.
    assert all(row["last_run"] is not None for row in results)
    assert results[0]["last_run"]["run_id"].endswith("-2")


@pytest.mark.django_db
def test_single_object_serializers_still_resolve_their_own_lookups(
    staff_client, settings, tmp_path
):
    """Batching keys off the page; a lone object must not inherit an empty map.

    Caching a map built from one row and handing it to the next is the obvious
    way to get this wrong, so the single-object paths are pinned here.
    """
    settings.MEDIA_ROOT = tmp_path
    _media_tweet(1, tmp_path)
    TwitterUser.objects.filter(handle="acct1").update(tracking=True)

    response = staff_client.patch(
        "/api/accounts/acct1/", {"priority": 2}, format="json"
    )
    assert response.status_code == 200
    assert response.json()["priority"] == 2
    assert response.json()["recent_tweet_count"] == 0

    timeline = staff_client.get("/api/accounts/acct1/tweets/")
    assert timeline.status_code == 200
    assert timeline.json()["results"][0]["media"][0]["url"].startswith("/media/")


def _seed_rows(count: int, user):
    """`count` of everything the list endpoints below page over.

    Deliberately built with empty `extras`, which is what a row stored before a
    key existed looks like. That is the state in which the tweet serializers
    fall back to the deferred `payload` column, so it is the state that exposes
    a per-row load.
    """
    for index in range(count):
        handle = f"acct{index}"
        TwitterUser.objects.create(handle=handle, tracking=True, priority=3)
        EndpointState.objects.create(
            account=handle, endpoint="UserTweets", data={"fetch_watermark": "2026-01-01"}
        )
        Tweet.objects.create(
            dedup_key=f"tweet:{index}", tweet_id=str(1000 + index), account=handle,
            text=f"post {index}", text_clean=f"post {index}", type="Tweet",
            source_subsystem="live",
        )
        ExportJob.objects.create(
            token=f"token-{index}", fmt="jsonl", params={}, requested_by=user,
            status="completed", relative_path=f"export-{index}.jsonl",
        )
    search = Search.objects.create(name="q", slug="q", raw_query="term")
    for index in range(count):
        hit = SearchTweet.objects.create(
            dedup_key=f"hit:{index}", tweet_id=str(5000 + index), account=f"acct{index}",
            text=f"hit {index}", text_clean=f"hit {index}", type="Tweet",
        )
        SearchHit.objects.create(search=search, search_tweet=hit, rank=index)
        FetchRun.objects.create(
            run_id=f"run-{index}", subsystem="search", search=search,
            status="completed", summary={"ingested_tweets": index, "new_tweets": index},
        )
    return search


def _clear():
    for model in (SearchHit, SearchTweet, Tweet, FetchRun, Search, ExportJob,
                  EndpointState, TwitterUser):
        model.objects.all().delete()


# Every list endpoint the console polls, keyed by a template the search id fills.
PAGED_ENDPOINTS = [
    "/api/feed/",
    "/api/accounts/",
    "/api/runs/",
    "/api/searches/",
    "/api/searches/{search}/results/",
    "/api/searches/{search}/runs/",
    "/api/export/",
    "/api/stats/pipeline/",
    "/api/stats/overview/",
    "/api/analytics/ingestion/",
    "/api/analytics/accounts/",
]


@pytest.mark.django_db
def test_no_list_endpoint_costs_more_queries_as_rows_are_added(
    staff_client, settings, tmp_path
):
    """The N+1 test proper: serve 2 rows, then 12, and compare the query counts.

    An upper bound catches a regression only if someone guessed the bound
    tightly enough; comparing two sizes catches any cost that scales, whatever
    the constant. This is how the feed's deferred-`payload` load was found --
    `.defer("payload")` avoids the blob on the list query, and then reading the
    deferred field re-fetched it one row at a time, so the page paid 30 extra
    round-trips *and* still loaded every payload.
    """
    settings.MEDIA_ROOT = tmp_path
    user = User.objects.get(username="ops")
    measured: dict[str, list[int]] = {}
    for row_count in (2, 12):
        _clear()
        search = _seed_rows(row_count, user)
        for template in PAGED_ENDPOINTS:
            with CaptureQueriesContext(connection) as queries:
                response = staff_client.get(template.format(search=search.id))
            assert response.status_code == 200, template
            measured.setdefault(template, []).append(len(queries.captured_queries))

    scaling = {
        template: counts for template, counts in measured.items() if counts[1] > counts[0]
    }
    assert not scaling, f"query count grows with row count: {scaling}"
