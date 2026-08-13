"""Unit tests: dedup key derivation and idempotent tweet upsert."""
import pytest

from apps.fetching.ingest import dedup_key, ingest_tweets, upsert_tweet
from apps.tweets.models import Tweet, TwitterUser


def _item(**over):
    base = {
        "rest_id": "111",
        "author_id": "999",
        "account": "@jack",
        "text": "hello",
        "created_at": "Wed Oct 10 20:19:24 +0000 2018",
        "likes": 5,
    }
    base.update(over)
    return base


def test_dedup_key_is_author_colon_tweet():
    assert dedup_key(_item()) == "999:111"


def test_dedup_key_falls_back_to_tweet_id_without_author():
    assert dedup_key(_item(author_id="")) == "111"


def test_dedup_key_empty_without_ids():
    assert dedup_key({"account": "@x"}) == ""


@pytest.mark.django_db
def test_upsert_is_idempotent_on_same_key():
    upsert_tweet(_item())
    upsert_tweet(_item(likes=42, text="updated"))  # same rest_id+author_id
    assert Tweet.objects.count() == 1
    t = Tweet.objects.get()
    assert t.dedup_key == "999:111"
    assert t.likes == 42  # update_or_create refreshed the row
    assert t.text == "updated"


@pytest.mark.django_db
def test_upsert_creates_and_links_author():
    upsert_tweet(_item())
    author = TwitterUser.objects.get(handle="jack")
    assert author.rest_id == "999"
    assert Tweet.objects.get().author_id == author.id


@pytest.mark.django_db
def test_upsert_enriches_author_from_normalized_payload():
    upsert_tweet(_item(author={
        "id": "999",
        "handle": "jack",
        "display_name": "Jack",
        "avatar_url": "https://img.example/jack.jpg",
        "verified": True,
        "verified_type": "Blue",
    }))

    author = TwitterUser.objects.get(handle="jack")
    assert author.display_name == "Jack"
    assert author.avatar_url == "https://img.example/jack.jpg"
    assert author.verified is True


@pytest.mark.django_db
def test_upsert_parses_rfc2822_created_at():
    t = upsert_tweet(_item())
    assert t.created_at is not None
    assert t.created_at.year == 2018 and t.created_at.tzinfo is not None


@pytest.mark.django_db
def test_ingest_tweets_counts_and_dedupes():
    items = [_item(rest_id="1"), _item(rest_id="2"), _item(rest_id="1")]
    assert ingest_tweets(items) == 3  # counts each successful upsert call
    assert Tweet.objects.count() == 2  # but only two distinct keys persist
