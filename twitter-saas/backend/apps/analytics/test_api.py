import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from apps.fetching.ingest import upsert_tweet
from apps.tweets.models import TwitterUser


@pytest.fixture
def client(db):
    user = User.objects.create_user(username="analytics", password="pw")
    api = APIClient()
    api.force_authenticate(user=user)
    return api


@pytest.mark.django_db
def test_overview_and_empty_analytics_are_available(client):
    TwitterUser.objects.create(handle="jack", tracking=True)
    upsert_tweet({
        "rest_id": "1", "author_id": "1", "account": "jack",
        "text": "A useful archive tweet", "created_at": "Wed Oct 10 20:19:24 +0000 2018",
    })

    overview = client.get("/api/stats/overview/")

    assert overview.status_code == 200
    assert overview.data["tweets"] == 1
    for path in (
        "/api/analytics/velocity/",
        "/api/analytics/topics/",
        "/api/analytics/accounts/",
        "/api/analytics/narratives/",
    ):
        assert client.get(path).status_code == 200
