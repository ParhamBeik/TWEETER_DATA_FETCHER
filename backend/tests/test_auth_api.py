"""Integration tests: register/login through DRF."""
import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from rest_framework.test import APIClient


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
