"""Rate limits on the endpoints where unlimited attempts are the problem.

Nothing in this API was throttled, which meant unlimited password guessing
against a system that holds a shared X session.

Throttles are installed but inert for the rest of the suite (config/settings_test
sets every rate to None, which short-circuits them) -- the counter lives in a
process-wide cache, so real rates would leak between unrelated tests, several of
which log in legitimately. The `throttled` fixture below turns them on for the
few cases where the behaviour is the point.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

PASSWORD = "correct-horse-battery"

# The production rates, restated here rather than imported so a change to them
# has to be made deliberately in both places.
RATES = {"anon": "60/min", "login": "5/min", "analytics": "30/min", "exports": "10/hour"}


@pytest.fixture
def throttled(monkeypatch):
    """Turn the installed throttles on, with a clean counter.

    Rates are patched onto the throttle class rather than through
    `override_settings`, because DRF binds `SimpleRateThrottle.THROTTLE_RATES`
    and `APIView.throttle_classes` at import time -- a settings override reloads
    `api_settings` but leaves both class attributes pointing at the objects they
    captured on first import, so it silently does nothing.
    """
    cache.clear()
    for scope, rate in RATES.items():
        monkeypatch.setitem(SimpleRateThrottle.THROTTLE_RATES, scope, rate)
    yield
    cache.clear()


@pytest.mark.django_db
def test_repeated_failed_logins_are_throttled(throttled):
    """The attack this exists to stop: guessing at machine speed."""
    User.objects.create_user(username="dave", password=PASSWORD)
    client = APIClient()

    for _ in range(5):
        response = client.post(
            "/api/auth/login/", {"username": "dave", "password": "wrong"}, format="json"
        )
        assert response.status_code == 400

    blocked = client.post(
        "/api/auth/login/", {"username": "dave", "password": "wrong"}, format="json"
    )
    assert blocked.status_code == 429


@pytest.mark.django_db
def test_the_throttle_counts_attempts_not_failures(throttled):
    """A correct password does not reset the budget -- otherwise an attacker
    with one valid account could clear the counter between guesses at another.
    """
    User.objects.create_user(username="dave", password=PASSWORD)
    client = APIClient()

    for _ in range(5):
        client.post(
            "/api/auth/login/", {"username": "dave", "password": PASSWORD}, format="json"
        )

    blocked = client.post(
        "/api/auth/login/", {"username": "dave", "password": PASSWORD}, format="json"
    )
    assert blocked.status_code == 429


@pytest.mark.django_db
def test_a_normal_sign_in_is_never_throttled(throttled):
    """Five a minute has to be invisible to a person typing a password."""
    User.objects.create_user(username="dave", password=PASSWORD)
    client = APIClient()

    first = client.post(
        "/api/auth/login/", {"username": "dave", "password": "typo"}, format="json"
    )
    assert first.status_code == 400
    second = client.post(
        "/api/auth/login/", {"username": "dave", "password": PASSWORD}, format="json"
    )
    assert second.status_code == 200


@pytest.mark.django_db
def test_the_console_is_not_throttled_by_its_own_polling(throttled):
    """The budget rail, ops and feed pollers make ~500 requests/hour per open
    tab. A signed-in user ceiling low enough to be useful would throttle the UI
    itself, so authenticated traffic is deliberately unmetered.
    """
    user = User.objects.create_user(username="operator", password=PASSWORD)
    client = APIClient()
    client.force_authenticate(user=user)

    for _ in range(80):
        assert client.get("/api/feed/").status_code == 200


@pytest.mark.django_db
def test_the_expensive_analytics_views_are_metered_even_when_signed_in(throttled):
    """These run trigram self-joins and phrase mining. Narratives already carries
    a statement timeout because one request could take a worker down; the
    throttle is what stops a retry loop simply re-triggering that.
    """
    user = User.objects.create_user(username="analyst", password=PASSWORD)
    client = APIClient()
    client.force_authenticate(user=user)

    codes = {client.get("/api/analytics/narratives/").status_code for _ in range(31)}
    assert 429 in codes


@pytest.mark.django_db
def test_all_expensive_analytics_views_are_metered(throttled):
    user = User.objects.create_user(username="analyst2", password=PASSWORD)
    client = APIClient()
    client.force_authenticate(user=user)

    for path in ["/api/analytics/ingestion/", "/api/analytics/accounts/"]:
        cache.clear()
        codes = {client.get(path).status_code for _ in range(31)}
        assert 429 in codes, f"{path} was not throttled"


@pytest.mark.django_db
def test_spoofed_x_forwarded_for_cannot_bypass_throttle(throttled, settings):
    settings.REST_FRAMEWORK["NUM_PROXIES"] = 1
    User.objects.create_user(username="dave2", password=PASSWORD)
    client = APIClient()

    for i in range(5):
        client.post(
            "/api/auth/login/",
            {"username": "dave2", "password": "wrong"},
            format="json",
            HTTP_X_FORWARDED_FOR=f"1.2.3.{i}, 10.0.0.1",
        )

    blocked = client.post(
        "/api/auth/login/",
        {"username": "dave2", "password": "wrong"},
        format="json",
        HTTP_X_FORWARDED_FOR="9.9.9.9, 10.0.0.1",
    )
    assert blocked.status_code == 429


@pytest.mark.django_db
def test_export_view_is_throttled(throttled):
    user = User.objects.create_user(username="exporter", password=PASSWORD)
    client = APIClient()
    client.force_authenticate(user=user)

    codes = [
        client.post("/api/export/", {"format": "jsonl"}, format="json").status_code
        for _ in range(11)
    ]
    assert 429 in codes
