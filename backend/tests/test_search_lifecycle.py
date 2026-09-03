"""Deleting a search has to take the whole job with it.

Integration-level (real ORM, real cascade rules) rather than unit: the thing
being asserted *is* the interaction between six tables and the key spellings the
engine writes them under, and a mocked repository would only prove the mock
agrees with itself. The one genuinely external edge -- revoking a broker message
-- is the only thing patched.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient

from fetching.ingest import ingest_search_hits
from fetching.searches import (
    endpoint_state_key,
    raw_page_key,
    schedule_for,
    teardown_search,
)
from tweets.models import (
    EndpointState,
    FetchRun,
    KeyValueState,
    RawPage,
    Search,
    SearchHit,
    SearchTweet,
)


@pytest.fixture
def staff_client(db):
    user = User.objects.create_user(username="op", password="pw", is_staff=True)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _search(slug="gold_watch", product="Latest", **kwargs):
    return Search.objects.create(
        name=slug, slug=slug, raw_query="gold", product=product, **kwargs
    )


def _hit(rest_id, account="stranger"):
    return {
        "rest_id": rest_id,
        "author_id": "42",
        "account": account,
        "text": f"post {rest_id}",
        "created_at": "Wed Oct 10 20:19:24 +0000 2018",
    }


def _furnish(search):
    """Give a search the full set of leftovers a few real runs would produce."""
    ingest_search_hits(search, [_hit("1"), _hit("2")])
    EndpointState.objects.create(
        account=endpoint_state_key(search),
        endpoint="SearchTimeline",
        data={"last_cursor": "DAABC"},
    )
    RawPage.objects.create(
        endpoint="SearchTimeline",
        account=raw_page_key(search),
        batch="batch_1",
        page_number=1,
        payload={"data": {}},
    )
    FetchRun.objects.create(
        run_id=f"run-{search.slug}", subsystem="search", target=search.slug, search=search
    )


@pytest.mark.django_db
def test_teardown_removes_every_trace_of_the_search():
    search = _search()
    _furnish(search)

    counts = teardown_search(search)

    assert not Search.objects.exists()
    assert not SearchHit.objects.exists()
    assert not SearchTweet.objects.exists()
    assert not EndpointState.objects.exists()
    assert not RawPage.objects.exists()
    assert not FetchRun.objects.exists()
    assert counts == {
        "revoked_queued_run": 0,
        "hits": 2,
        "search_tweets": 2,
        "endpoint_state": 1,
        "fetch_runs": 1,
        "raw_pages": 1,
    }


@pytest.mark.django_db
def test_teardown_keeps_a_body_another_search_still_wants():
    """Two queries can match one post; only the unwanted copy goes."""
    doomed, kept = _search("doomed"), _search("kept")
    ingest_search_hits(doomed, [_hit("shared"), _hit("only-mine")])
    ingest_search_hits(kept, [_hit("shared")])

    counts = teardown_search(doomed)

    assert counts["search_tweets"] == 1
    assert [t.tweet_id for t in SearchTweet.objects.all()] == ["shared"]
    assert SearchHit.objects.filter(search=kept).count() == 1


@pytest.mark.django_db
def test_teardown_leaves_another_searchs_state_alone():
    """The two key spellings must be exact, or a delete hits the wrong query.

    The engine writes pagination state under "<slug>::<product>" and raw pages
    under "<slug>:<product>" -- one colon versus two. Getting either wrong makes
    the delete look successful while leaving a live cursor behind.
    """
    doomed, neighbour = _search("alpha"), _search("beta")
    _furnish(doomed)
    _furnish(neighbour)

    teardown_search(doomed)

    assert EndpointState.objects.get().account == endpoint_state_key(neighbour)
    assert RawPage.objects.get().account == raw_page_key(neighbour)
    assert FetchRun.objects.get().search_id == neighbour.id


@pytest.mark.django_db
def test_teardown_revokes_a_run_still_sitting_in_the_queue():
    search = _search(queued_task_id="task-abc")
    with patch("config.celery.app.control.revoke") as revoke:
        counts = teardown_search(search)
    revoke.assert_called_once_with("task-abc")
    assert counts["revoked_queued_run"] == 1


@pytest.mark.django_db
def test_teardown_survives_an_unreachable_broker():
    """A delete must not be blocked by the queue being down.

    Worst case the revoke is missed, the task starts, finds no Search row and
    returns -- which run_search already handles.
    """
    search = _search(queued_task_id="task-abc")
    with patch("config.celery.app.control.revoke", side_effect=OSError("no broker")):
        counts = teardown_search(search)
    assert counts["revoked_queued_run"] == 0
    assert not Search.objects.exists()


@pytest.mark.django_db
def test_teardown_spares_the_shared_request_state():
    """Rate limits and endpoint health are one row for the whole subsystem."""
    search = _search()
    KeyValueState.objects.create(
        namespace="request_state", name="search:rate_limits.json", data={"SearchTimeline": {}}
    )

    teardown_search(search)

    assert KeyValueState.objects.filter(namespace="request_state").exists()


# --- schedule ---------------------------------------------------------------


@pytest.mark.django_db
def test_schedule_reports_a_never_run_search_as_due_now():
    assert schedule_for(_search())["is_due"] is True


@pytest.mark.django_db
def test_schedule_counts_down_to_the_next_run():
    search = _search(interval_seconds=1800)
    search.last_run_at = timezone.now() - timedelta(seconds=600)
    search.save(update_fields=["last_run_at"])

    schedule = schedule_for(search)

    assert schedule["state"] == "idle"
    assert schedule["is_due"] is False
    assert 1100 <= schedule["seconds_until_due"] <= 1200


@pytest.mark.django_db
@pytest.mark.parametrize(
    "kwargs, running, expected",
    [
        ({"enabled": False}, False, "paused"),
        ({"queued_task_id": "abc"}, False, "queued"),
        ({"queued_task_id": "abc"}, True, "running"),
        ({}, False, "idle"),
    ],
)
def test_schedule_state_names_what_the_search_is_doing(kwargs, running, expected):
    # A paused search reads "paused" even while a run finishes, because the
    # question the dot answers is "will this keep collecting", not "is a
    # subprocess alive".
    assert schedule_for(_search(**kwargs), running=running)["state"] == expected


# --- API --------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_endpoint_reports_what_it_removed(staff_client):
    search = _search()
    _furnish(search)

    body = staff_client.delete(f"/api/searches/{search.id}/").data

    assert body["hits"] == 2
    assert body["raw_pages"] == 1
    assert not Search.objects.exists()


@pytest.mark.django_db
def test_delete_is_staff_only(db):
    search = _search()
    client = APIClient()
    client.force_authenticate(user=User.objects.create_user("reader", password="pw"))

    assert client.delete(f"/api/searches/{search.id}/").status_code == 403
    assert Search.objects.exists()


@pytest.mark.django_db
def test_pause_stops_the_schedule_without_losing_results(staff_client):
    search = _search()
    ingest_search_hits(search, [_hit("1")])

    body = staff_client.post(f"/api/searches/{search.id}/pause/").data

    assert body["state"] == "paused"
    assert body["is_due"] is False
    assert SearchHit.objects.filter(search=search).count() == 1


@pytest.mark.django_db
def test_patch_edits_the_query_without_a_teardown(staff_client):
    search = _search()
    ingest_search_hits(search, [_hit("1")])

    resp = staff_client.patch(
        f"/api/searches/{search.id}/", {"interval_seconds": 600}, format="json"
    )

    assert resp.status_code == 200
    search.refresh_from_db()
    assert search.interval_seconds == 600
    assert SearchHit.objects.count() == 1


@pytest.mark.django_db
def test_runs_endpoint_scopes_history_to_one_search(staff_client):
    mine, other = _search("mine"), _search("other")
    FetchRun.objects.create(run_id="a", subsystem="search", search=mine)
    FetchRun.objects.create(run_id="b", subsystem="search", search=other)

    body = staff_client.get(f"/api/searches/{mine.id}/runs/").data

    assert [row["run_id"] for row in body["results"]] == ["a"]


@pytest.mark.django_db
def test_list_reports_hit_count_and_schedule_per_row(staff_client):
    search = _search()
    ingest_search_hits(search, [_hit("1"), _hit("2")])

    row = staff_client.get("/api/searches/").data["results"][0]

    assert row["hit_count"] == 2
    assert row["schedule"]["state"] == "idle"
    assert row["schedule"]["is_due"] is True


@pytest.mark.django_db
def test_results_page_newest_first_and_never_duplicate_a_hit(staff_client):
    """One row per tweet, newest first — the order the paginator actually applies."""
    search = _search()
    ingest_search_hits(search, [_hit("old"), _hit("new")])
    SearchTweet.objects.filter(tweet_id="old").update(
        created_at=timezone.now() - timedelta(days=2)
    )
    SearchTweet.objects.filter(tweet_id="new").update(created_at=timezone.now())
    # A second query matching the same posts must not double them up here.
    ingest_search_hits(_search("other"), [_hit("old"), _hit("new")])

    ids = [
        row["tweet_id"]
        for row in staff_client.get(f"/api/searches/{search.id}/results/").data["results"]
    ]

    assert ids == ["new", "old"]


@pytest.mark.django_db
def test_django_admin_delete_triggers_teardown(rf):
    from django.contrib.admin.sites import AdminSite
    from tweets.admin import SearchAdmin

    search = _search("admin_del")
    _furnish(search)

    admin_instance = SearchAdmin(Search, AdminSite())
    request = rf.post("/admin/tweets/search/")
    with patch("fetching.searches.revoke_queued_run") as revoke:
        admin_instance.delete_model(request, search)
        revoke.assert_called_once_with(search)

    assert not Search.objects.filter(slug="admin_del").exists()
    assert not EndpointState.objects.filter(account=endpoint_state_key(search)).exists()
    assert not RawPage.objects.filter(account=raw_page_key(search)).exists()
