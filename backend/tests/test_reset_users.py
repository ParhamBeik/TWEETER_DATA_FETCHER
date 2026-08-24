"""reset_users: destructive, so it must default to doing nothing.

Integration level -- the point is what survives in the database.
"""
import pytest
from django.contrib.auth.models import User
from django.core.management import call_command

from tweets.models import Tweet, TwitterUser, XSession
from fetching.ingest import upsert_tweet


@pytest.mark.django_db
def test_without_yes_it_only_reports():
    User.objects.create_user(username="carol", password="pw")

    call_command("reset_users")

    assert User.objects.count() == 1


@pytest.mark.django_db
def test_with_yes_it_clears_every_account():
    User.objects.create_user(username="carol", password="pw")
    User.objects.create_superuser(username="root", password="pw")

    call_command("reset_users", "--yes")

    assert User.objects.count() == 0


@pytest.mark.django_db
def test_it_leaves_the_collected_data_and_the_x_session_alone():
    """Login accounts are not the archive; wiping one must not touch the other."""
    User.objects.create_user(username="carol", password="pw")
    TwitterUser.objects.create(handle="business", tracking=True)
    upsert_tweet({"rest_id": "1", "account": "business", "text": "t"})
    XSession.objects.create(name="default", cookies={"auth_token": "x"}, active=True)

    call_command("reset_users", "--yes")

    assert TwitterUser.objects.count() == 1
    assert Tweet.objects.count() == 1
    assert XSession.objects.get(name="default").active is True
