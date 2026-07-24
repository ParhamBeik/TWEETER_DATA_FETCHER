"""Integration tests: feed merge/order and search create enqueue, through DRF."""
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from apps.accounts.models import Follow, SearchSubscription
from apps.fetching.ingest import upsert_tweet
from apps.tweets.models import Search, SearchResult, Tweet, TwitterUser


@pytest.fixture
def client_user(db):
    user = User.objects.create_user(username="alice", password="pw")
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def _tweet(account, rest_id, created):
    return upsert_tweet(
        {
            "rest_id": rest_id,
            "author_id": "1",
            "account": account,
            "text": f"t{rest_id}",
            "created_at": created,
        }
    )


@pytest.mark.django_db
def test_feed_merges_follows_and_searches_ordered_desc(client_user):
    client, user = client_user
    acct = TwitterUser.objects.create(handle="jack", tracking=True)
    Follow.objects.create(user=user, account=acct)

    older = _tweet("jack", "1", "Wed Oct 10 20:19:24 +0000 2018")
    newer = _tweet("jack", "2", "Wed Oct 10 21:19:24 +0000 2018")

    # A tweet reachable only via a subscribed search must also appear.
    search = Search.objects.create(name="ai", slug="ai", raw_query="ai", owner=user)
    SearchSubscription.objects.create(user=user, search=search)
    via_search = _tweet("someoneelse", "3", "Wed Oct 10 22:19:24 +0000 2018")
    SearchResult.objects.create(search=search, tweet=via_search, rank=0)

    resp = client.get("/api/feed/")
    assert resp.status_code == 200
    ids = [t["tweet_id"] for t in resp.data["results"]]
    assert ids == ["3", "2", "1"]  # newest first, merged, no dupes


@pytest.mark.django_db
def test_feed_dedupes_tweet_in_both_follow_and_search(client_user):
    client, user = client_user
    acct = TwitterUser.objects.create(handle="jack", tracking=True)
    Follow.objects.create(user=user, account=acct)
    t = _tweet("jack", "9", "Wed Oct 10 20:19:24 +0000 2018")
    search = Search.objects.create(name="s", slug="s", raw_query="s", owner=user)
    SearchSubscription.objects.create(user=user, search=search)
    SearchResult.objects.create(search=search, tweet=t, rank=0)

    resp = client.get("/api/feed/")
    assert [x["tweet_id"] for x in resp.data["results"]] == ["9"]  # once, not twice


@pytest.mark.django_db
def test_feed_requires_auth():
    assert APIClient().get("/api/feed/").status_code == 401


@pytest.mark.django_db
def test_account_timeline_filters_by_handle(client_user):
    client, _ = client_user
    _tweet("jack", "1", "Wed Oct 10 20:19:24 +0000 2018")
    _tweet("elon", "2", "Wed Oct 10 20:19:24 +0000 2018")
    resp = client.get("/api/accounts/jack/tweets/")
    assert [t["tweet_id"] for t in resp.data["results"]] == ["1"]


@pytest.mark.django_db
def test_create_search_enqueues_and_subscribes(client_user):
    client, user = client_user
    with patch("apps.fetching.tasks.run_search.delay") as delay:
        resp = client.post(
            "/api/searches/", {"raw_query": "openai", "product": "Latest"}, format="json"
        )
    assert resp.status_code == 201
    search = Search.objects.get(raw_query="openai")
    assert search.owner == user and search.slug  # slug auto-derived
    assert SearchSubscription.objects.filter(user=user, search=search).exists()
    delay.assert_called_once_with(search.id)


@pytest.mark.django_db
def test_search_list_filters_by_product(client_user):
    client, user = client_user
    Search.objects.create(name="a", slug="a", raw_query="a", product="Top", owner=user)
    Search.objects.create(name="b", slug="b", raw_query="b", product="Latest", owner=user)
    resp = client.get("/api/searches/?product=Latest")
    assert [s["raw_query"] for s in resp.data["results"]] == ["b"]


@pytest.mark.django_db
def test_search_list_does_not_leak_other_users(client_user):
    client, user = client_user
    other = User.objects.create_user(username="eve", password="pw")
    Search.objects.create(name="mine", slug="mine", raw_query="mine", owner=user)
    Search.objects.create(name="eves", slug="eves", raw_query="eves", owner=other)
    resp = client.get("/api/searches/")
    assert [s["raw_query"] for s in resp.data["results"]] == ["mine"]


@pytest.mark.django_db
def test_search_create_derives_name_from_raw_query(client_user):
    client, _ = client_user
    with patch("apps.fetching.tasks.run_search.delay"):
        resp = client.post(
            "/api/searches/", {"raw_query": "openai", "product": "Latest"}, format="json"
        )
    assert resp.status_code == 201
    search = Search.objects.get(raw_query="openai")
    assert search.name == "openai"  # derived from raw_query when name omitted
