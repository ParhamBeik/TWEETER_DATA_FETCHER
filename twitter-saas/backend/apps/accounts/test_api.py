"""Integration tests: register/login and follow auto-enqueue, through DRF."""
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
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
@override_settings(ALLOW_REGISTRATION=False)
def test_register_disabled_returns_403():
    resp = APIClient().post(
        "/api/auth/register/", {"username": "carol", "password": "pw"}, format="json"
    )
    assert resp.status_code == 403
    assert not User.objects.filter(username="carol").exists()


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
        resp = client.post("/api/follows/", {"handle": "@Jack"}, format="json")
    assert resp.status_code == 201
    acct = TwitterUser.objects.get(handle="jack")
    assert acct.tracking is True
    assert Follow.objects.filter(user=user, account=acct).exists()
    hist.assert_called_once_with("jack")
    live.assert_called_once_with("jack")


@pytest.mark.django_db
def test_follow_already_tracking_does_not_reenqueue(auth_client):
    client, _user = auth_client
    TwitterUser.objects.create(handle="jack", tracking=True)
    with patch("apps.fetching.tasks.fetch_account_historical.delay") as hist, patch(
        "apps.fetching.tasks.fetch_account_live.delay"
    ) as live:
        client.post("/api/follows/", {"handle": "jack"}, format="json")
    hist.assert_not_called()
    live.assert_not_called()


@pytest.mark.django_db
def test_refollow_after_untrack_reenqueues(auth_client):
    client, _user = auth_client
    TwitterUser.objects.create(handle="jack", tracking=False)
    with patch("apps.fetching.tasks.fetch_account_historical.delay") as hist, patch(
        "apps.fetching.tasks.fetch_account_live.delay"
    ) as live:
        resp = client.post("/api/follows/", {"handle": "jack"}, format="json")
    assert resp.status_code == 201
    assert TwitterUser.objects.get(handle="jack").tracking is True
    hist.assert_called_once_with("jack")
    live.assert_called_once_with("jack")


@pytest.mark.django_db
def test_unfollow_untracks_when_no_followers_remain(auth_client):
    client, user = auth_client
    acct = TwitterUser.objects.create(handle="jack", tracking=True)
    Follow.objects.create(user=user, account=acct)
    resp = client.delete("/api/follows/", {"handle": "jack"}, format="json")
    assert resp.status_code == 204
    assert not Follow.objects.filter(user=user, account=acct).exists()
    acct.refresh_from_db()
    assert acct.tracking is False


@pytest.mark.django_db
def test_unfollow_keeps_tracking_if_another_follower(auth_client):
    client, user = auth_client
    other = User.objects.create_user(username="other", password="pw")
    acct = TwitterUser.objects.create(handle="jack", tracking=True)
    Follow.objects.create(user=user, account=acct)
    Follow.objects.create(user=other, account=acct)
    client.delete("/api/follows/", {"handle": "jack"}, format="json")
    acct.refresh_from_db()
    assert acct.tracking is True
    assert Follow.objects.filter(account=acct).count() == 1
