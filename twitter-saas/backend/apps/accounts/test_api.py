"""Integration tests: register/login and follow auto-enqueue, through DRF."""
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from apps.accounts.models import Follow
from apps.tweets.models import TwitterUser


@pytest.fixture
def auth_client(db):
    user = User.objects.create_user(username="bob", password="pw")
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


@pytest.mark.django_db
def test_register_returns_token_and_creates_user():
    resp = APIClient().post(
        "/api/auth/register/", {"username": "carol", "password": "pw"}, format="json"
    )
    assert resp.status_code == 201 and resp.data["token"]
    assert User.objects.filter(username="carol").exists()


@pytest.mark.django_db
def test_login_rejects_bad_credentials():
    User.objects.create_user(username="dave", password="right")
    resp = APIClient().post(
        "/api/auth/login/", {"username": "dave", "password": "wrong"}, format="json"
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_follow_new_handle_tracks_and_enqueues_initial_fetch(auth_client):
    client, user = auth_client
    with patch("apps.fetching.tasks.fetch_account_historical.delay") as hist, patch(
        "apps.fetching.tasks.fetch_account_live.delay"
    ) as live:
        resp = client.post("/api/follows/", {"handle": "@jack"}, format="json")
    assert resp.status_code == 201
    acct = TwitterUser.objects.get(handle="jack")
    assert acct.tracking is True  # following forces tracking on
    assert Follow.objects.filter(user=user, account=acct).exists()
    hist.assert_called_once_with("jack")  # initial backfill + live poll queued
    live.assert_called_once_with("jack")


@pytest.mark.django_db
def test_follow_existing_account_does_not_reenqueue(auth_client):
    client, user = auth_client
    TwitterUser.objects.create(handle="jack", tracking=True)
    with patch("apps.fetching.tasks.fetch_account_historical.delay") as hist, patch(
        "apps.fetching.tasks.fetch_account_live.delay"
    ) as live:
        client.post("/api/follows/", {"handle": "jack"}, format="json")
    hist.assert_not_called()  # account already existed -> no first-time fetch
    live.assert_not_called()


@pytest.mark.django_db
def test_unfollow_removes_link(auth_client):
    client, user = auth_client
    acct = TwitterUser.objects.create(handle="jack", tracking=True)
    Follow.objects.create(user=user, account=acct)
    resp = client.delete("/api/follows/", {"handle": "jack"}, format="json")
    assert resp.status_code == 204
    assert not Follow.objects.filter(user=user, account=acct).exists()
