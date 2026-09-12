"""The signup/login/refresh/logout round trip, and what a new account may do.

Integration level throughout: these assert the contract the frontend codes
against and the boundary that keeps an open signup from handing out control of
the shared X session, neither of which survives being mocked.
"""
from unittest.mock import MagicMock

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from rest_framework.test import APIClient

GOOD_PASSWORD = "correct-horse-battery"


def register(client=None, **overrides):
    payload = {"username": "carol", "email": "carol@example.com", "password": GOOD_PASSWORD}
    payload.update(overrides)
    return (client or APIClient()).post("/api/auth/register/", payload, format="json")


def authed(user) -> APIClient:
    client = APIClient()
    resp = client.post(
        "/api/auth/login/", {"username": user.username, "password": GOOD_PASSWORD}, format="json"
    )
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['access']}")
    return client


# --- Registration -----------------------------------------------------------


@pytest.mark.django_db
def test_register_returns_a_token_pair_and_creates_a_non_staff_user():
    resp = register()

    assert resp.status_code == 201
    assert resp.data["access"] and resp.data["refresh"]
    assert resp.data["user"]["is_staff"] is False
    # A new signup must not arrive able to operate the fetcher.
    assert not User.objects.get(username="carol").is_staff


@pytest.mark.django_db
def test_register_enforces_the_real_password_validators():
    resp = register(password="pw")

    assert resp.status_code == 400
    assert "password" in resp.data["errors"]
    assert not User.objects.filter(username="carol").exists()


@pytest.mark.django_db
def test_register_rejects_a_common_password():
    resp = register(password="password123")

    assert resp.status_code == 400
    assert "password" in resp.data["errors"]


@pytest.mark.django_db
def test_register_rejects_a_duplicate_username_regardless_of_case():
    """Otherwise Carol and carol both exist and a later login is ambiguous."""
    register()

    resp = register(username="CAROL", email="other@example.com")

    assert resp.status_code == 400
    assert "username" in resp.data["errors"]


@pytest.mark.django_db
def test_register_rejects_a_malformed_email():
    resp = register(email="not-an-email")

    assert resp.status_code == 400
    assert "email" in resp.data["errors"]


@pytest.mark.django_db
def test_register_allows_a_blank_email():
    assert register(email="").status_code == 201


@pytest.mark.django_db
@override_settings(ALLOW_REGISTRATION=False)
def test_register_can_be_closed():
    resp = register()

    assert resp.status_code == 403
    assert not User.objects.filter(username="carol").exists()


# --- Login, refresh, logout -------------------------------------------------


@pytest.mark.django_db
def test_login_returns_a_token_pair():
    User.objects.create_user(username="dave", password=GOOD_PASSWORD)

    resp = APIClient().post(
        "/api/auth/login/", {"username": "dave", "password": GOOD_PASSWORD}, format="json"
    )

    assert resp.status_code == 200
    assert resp.data["access"] and resp.data["refresh"]


@pytest.mark.django_db
def test_login_does_not_reveal_whether_the_username_exists():
    User.objects.create_user(username="dave", password=GOOD_PASSWORD)

    wrong_password = APIClient().post(
        "/api/auth/login/", {"username": "dave", "password": "nope"}, format="json"
    )
    no_such_user = APIClient().post(
        "/api/auth/login/", {"username": "nobody", "password": "nope"}, format="json"
    )

    assert wrong_password.status_code == no_such_user.status_code == 400
    assert wrong_password.data["detail"] == no_such_user.data["detail"]


@pytest.mark.django_db
def test_an_inactive_user_cannot_log_in():
    User.objects.create_user(username="dave", password=GOOD_PASSWORD, is_active=False)

    resp = APIClient().post(
        "/api/auth/login/", {"username": "dave", "password": GOOD_PASSWORD}, format="json"
    )

    assert resp.status_code == 400


@pytest.mark.django_db
def test_refresh_rotates_the_token_and_kills_the_one_it_replaced():
    """A replayed refresh token must be dead, or stealing one is permanent access."""
    first = register().data["refresh"]

    rotated = APIClient().post("/api/auth/refresh/", {"refresh": first}, format="json")
    assert rotated.status_code == 200
    assert rotated.data["access"]
    assert rotated.data["refresh"] != first

    replayed = APIClient().post("/api/auth/refresh/", {"refresh": first}, format="json")
    assert replayed.status_code == 401


@pytest.mark.django_db
def test_a_refreshed_access_token_actually_works():
    tokens = register().data
    rotated = APIClient().post(
        "/api/auth/refresh/", {"refresh": tokens["refresh"]}, format="json"
    ).data

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {rotated['access']}")

    assert client.get("/api/auth/me/").status_code == 200


@pytest.mark.django_db
def test_logout_blacklists_the_refresh_token():
    refresh = register().data["refresh"]

    assert APIClient().post("/api/auth/logout/", {"refresh": refresh}, format="json").status_code == 204
    assert APIClient().post("/api/auth/refresh/", {"refresh": refresh}, format="json").status_code == 401


@pytest.mark.django_db
def test_logging_out_twice_is_not_an_error():
    """The caller wanted the token dead; on the second call it already is."""
    refresh = register().data["refresh"]
    APIClient().post("/api/auth/logout/", {"refresh": refresh}, format="json")

    assert APIClient().post("/api/auth/logout/", {"refresh": refresh}, format="json").status_code == 204


@pytest.mark.django_db
def test_me_reports_the_identity_the_console_renders_from():
    register()
    user = User.objects.get(username="carol")

    resp = authed(user).get("/api/auth/me/")

    assert resp.status_code == 200
    assert resp.data["username"] == "carol"
    assert resp.data["is_staff"] is False


@pytest.mark.django_db
def test_an_anonymous_request_is_rejected():
    assert APIClient().get("/api/auth/me/").status_code == 401


# --- What a new account may do ----------------------------------------------


@pytest.mark.django_db
def test_a_new_signup_cannot_replace_the_shared_x_session():
    """The hole open signup would otherwise widen: anyone who registers could
    swap the one operator session every fetch runs on."""
    register()
    client = authed(User.objects.get(username="carol"))

    resp = client.post(
        "/api/session/",
        {"cookies": {"auth_token": "x", "ct0": "y"}, "headers": {"authorization": "Bearer z"}},
        format="json",
    )

    assert resp.status_code == 403


@pytest.mark.django_db
def test_a_new_signup_cannot_read_session_health_either():
    register()

    assert authed(User.objects.get(username="carol")).get("/api/session/").status_code == 403


@pytest.mark.django_db
def test_a_new_signup_cannot_trigger_a_fetch_cycle():
    register()

    resp = authed(User.objects.get(username="carol")).post(
        "/api/cycles/", {"subsystem": "live"}, format="json"
    )

    assert resp.status_code == 403


@pytest.mark.django_db
def test_a_new_signup_can_read_the_archive():
    register()
    client = authed(User.objects.get(username="carol"))

    assert client.get("/api/feed/").status_code == 200
    assert client.get("/api/accounts/").status_code == 200


@pytest.mark.django_db
def test_a_staff_user_can_operate_the_fetcher():
    register()
    user = User.objects.get(username="carol")
    user.is_staff = True
    user.save(update_fields=["is_staff"])

    assert authed(user).get("/api/session/").status_code == 200


# --- Public probes the signed-out UI and the deploy healthcheck depend on ---


@pytest.mark.django_db
def test_health_is_public_and_ok():
    """Integration: compose curls this URL; it must work with no session."""
    resp = APIClient().get("/api/health/")

    assert resp.status_code == 200
    assert resp.data == {"status": "ok"}


@pytest.mark.django_db
def test_health_ignores_a_garbage_bearer_token():
    """A probe must not 401 because JWT authentication ran on it."""
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer not-a-token")
    assert client.get("/api/health/").status_code == 200


@pytest.mark.django_db
def test_health_is_unhealthy_when_the_database_does_not_answer(monkeypatch):
    """Unit: the deploy probe must 503 without naming the exception."""
    monkeypatch.setattr("tweets.auth_views.probe_database", lambda: False)
    resp = APIClient().get("/api/health/")

    assert resp.status_code == 503
    assert resp.data == {"status": "unhealthy"}


@pytest.mark.django_db
def test_health_is_unhealthy_when_the_broker_does_not_answer(monkeypatch):
    monkeypatch.setattr("tweets.auth_views.probe_broker", lambda: False)
    resp = APIClient().get("/api/health/")

    assert resp.status_code == 503
    assert resp.data == {"status": "unhealthy"}


@override_settings(CELERY_BROKER_URL="redis://broker:6379/0")
def test_queue_health_reports_named_and_legacy_depths(monkeypatch):
    from fetching.health import queue_health

    client = MagicMock()
    client.pipeline.return_value.execute.return_value = [0, 1, 2, 3, 4]
    monkeypatch.setattr("fetching.health.Redis.from_url", lambda *args, **kwargs: client)

    health = queue_health()

    assert health["depths"] == {
        "live": 0, "historical": 1, "search": 2, "control": 3, "celery": 4,
    }
    assert health["unexpected_default"] == 4


def test_auth_config_reports_open_registration():
    resp = APIClient().get("/api/auth/config/")

    assert resp.status_code == 200
    assert resp.data == {"allow_registration": True}


@override_settings(ALLOW_REGISTRATION=False)
def test_auth_config_reports_closed_registration():
    resp = APIClient().get("/api/auth/config/")

    assert resp.status_code == 200
    assert resp.data == {"allow_registration": False}


@pytest.mark.django_db
def test_signing_in_records_last_login():
    """SIMPLE_JWT's UPDATE_LAST_LOGIN only applies inside TokenObtainSerializer,
    which LoginView does not use. Without an explicit call, last_login stays null
    for every account forever and /admin/ cannot show who has actually signed in.
    """
    user = User.objects.create_user(username="erin", password=GOOD_PASSWORD)
    assert user.last_login is None

    resp = APIClient().post(
        "/api/auth/login/", {"username": "erin", "password": GOOD_PASSWORD}, format="json"
    )

    assert resp.status_code == 200
    user.refresh_from_db()
    assert user.last_login is not None


@pytest.mark.django_db
def test_a_rejected_sign_in_does_not_record_last_login():
    user = User.objects.create_user(username="erin2", password=GOOD_PASSWORD)

    APIClient().post(
        "/api/auth/login/", {"username": "erin2", "password": "wrong"}, format="json"
    )

    user.refresh_from_db()
    assert user.last_login is None
