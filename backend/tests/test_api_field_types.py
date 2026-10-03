"""Wrong-typed *fields* inside a well-formed JSON object must be a 4xx.

`test_api_input_sweep.py` replays hostile values per route and parameter, and
`body_mapping` rejects a body that is not an object. Neither caught a list where
a string belongs: the password hashers and validators raise on it, and so did a
nested session field and an over-long name on Postgres.
"""
import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from tweets.models import TwitterUser

PASSWORD = "Sup3r-long-pass!!"

CASES = [
    ("anon", "post", "/api/auth/register/", {"username": "u1", "password": 12345678901}),
    ("anon", "post", "/api/auth/register/", {"username": "u2", "password": ["x"]}),
    ("anon", "post", "/api/auth/register/", {"username": "u3", "password": {"a": 1}}),
    ("anon", "post", "/api/auth/register/", {"username": ["a"], "password": PASSWORD}),
    ("anon", "post", "/api/auth/login/", {"username": "nobody", "password": ["x"]}),
    ("anon", "post", "/api/auth/login/", {"username": "nobody", "password": 5}),
    ("anon", "post", "/api/auth/login/", {"username": "existing", "password": ["x"]}),
    ("anon", "post", "/api/auth/login/", {"username": "existing", "password": {"a": 1}}),
    ("anon", "post", "/api/auth/logout/", {"refresh": ["x"]}),
    ("anon", "post", "/api/auth/refresh/", {"refresh": {"a": 1}}),
    ("staff", "post", "/api/session/", {"api_cookies": {"ct0": "x"}, "api_headers": [1]}),
    ("staff", "post", "/api/session/", {"api_cookies": {"ct0": "x"}, "api_auth": "str"}),
    ("staff", "post", "/api/session/", {"api_cookies": ["x"]}),
    ("staff", "post", "/api/accounts/", {"handle": ["x"]}),
    ("staff", "post", "/api/accounts/", {"handle": "x" * 500}),
    ("staff", "post", "/api/accounts/", {"handle": "ok", "display_name": {"a": 1}}),
    ("staff", "post", "/api/accounts/", {"handle": "ok2", "display_name": "x" * 5000}),
    ("staff", "patch", "/api/accounts/existing/", {"display_name": "x" * 5000}),
    ("staff", "patch", "/api/accounts/existing/", {"display_name": ["x"]}),
]


@pytest.mark.django_db
@pytest.mark.parametrize("who,method,path,body", CASES)
def test_wrong_typed_fields_are_rejected_not_crashed(who, method, path, body):
    User.objects.create_user("existing", password=PASSWORD)
    TwitterUser.objects.create(handle="existing", display_name="E")
    client = APIClient(raise_request_exception=False)
    if who == "staff":
        client.force_authenticate(User.objects.create_user("st", password=PASSWORD, is_staff=True))

    response = getattr(client, method)(path, body, format="json")

    assert response.status_code < 500, (response.status_code, response.content[:300])


@pytest.mark.django_db
def test_a_non_string_password_is_an_ordinary_failed_sign_in():
    User.objects.create_user("existing", password=PASSWORD)
    response = APIClient().post(
        "/api/auth/login/", {"username": "existing", "password": [PASSWORD]}, format="json"
    )
    assert response.status_code == 400
    assert response.data["detail"] == "Incorrect username or password."


@pytest.mark.django_db
def test_an_over_long_display_name_is_clipped_to_the_column():
    staff = User.objects.create_user("st", password=PASSWORD, is_staff=True)
    client = APIClient()
    client.force_authenticate(staff)
    response = client.post("/api/accounts/", {"handle": "ok", "display_name": "x" * 5000}, format="json")
    assert response.status_code == 201
    assert len(TwitterUser.objects.get(handle="ok").display_name) == 255
