"""Regression tests for the console defects found in the August 2026 QA pass.

Every test here pins one behaviour that was previously wrong on the live site,
so the fix cannot be undone silently. Grouped by the screen that showed it.
"""
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient

from fetching.ingest import ingest_tweets, upsert_tweet
from tweets.models import Tweet, TwitterUser


@pytest.fixture
def client_user(db):
    user = User.objects.create_user("qa", password="qa-pass-12345", is_staff=True)
    client = APIClient()
    client.force_authenticate(user)
    return client


def _item(tweet_id, account="alpha", **overrides):
    item = {
        "rest_id": tweet_id,
        "id": tweet_id,
        "author_id": f"uid-{account}",
        "account": account,
        "author": {"handle": account, "display_name": account},
        "text": f"post {tweet_id}",
        "url": f"https://x.com/{account}/status/{tweet_id}",
        "type": "Tweet",
        "created_at": "Fri Aug 28 12:00:00 +0000 2026",
        "likes": 0,
        "retweets": 0,
        "replies": 0,
        "quotes": 0,
        "bookmarks": 0,
        "views": 0,
    }
    item.update(overrides)
    return item


def _track(handle, priority=1):
    return TwitterUser.objects.update_or_create(
        handle=handle, defaults={"tracking": True, "priority": priority}
    )[0]


# --- Feed ranking -----------------------------------------------------------


def test_most_engaged_ignores_views(client_user):
    """Views are reach, not engagement, and must not decide `sort=top`.

    The live bug: engagement was likes+retweets+views, so a post with 7.3M views
    and 47k likes outranked one with 5.3M views and 48k likes. Whichever post was
    shown to more people won, regardless of what anyone did about it.
    """
    _track("alpha")
    upsert_tweet(_item("1", likes=48_000, retweets=6_000, views=5_300_000))
    upsert_tweet(_item("2", likes=47_000, retweets=4_800, views=7_300_000))

    top = client_user.get("/api/feed/?sort=top").json()["results"]
    assert [row["tweet_id"] for row in top] == ["1", "2"]

    # ...and reach is still available, as its own question.
    viewed = client_user.get("/api/feed/?sort=views").json()["results"]
    assert [row["tweet_id"] for row in viewed] == ["2", "1"]


def test_engagement_counts_replies_and_quotes(client_user):
    """A conversation is engagement even when nobody liked or reposted it."""
    _track("alpha")
    upsert_tweet(_item("quiet", likes=10, retweets=0, replies=0, quotes=0))
    upsert_tweet(_item("talked", likes=0, retweets=0, replies=40, quotes=5))
    top = client_user.get("/api/feed/?sort=top").json()["results"]
    assert [row["tweet_id"] for row in top] == ["talked", "quiet"]


# --- Archive search ---------------------------------------------------------


@pytest.mark.parametrize("query", ["the", "and", "a"])
def test_search_matches_common_words(client_user, query):
    """Full-text search returned nothing for stop words. A search box should not."""
    _track("alpha")
    upsert_tweet(_item("1", text="the quick brown fox and a dog"))
    results = client_user.get(f"/api/feed/?q={query}").json()["results"]
    assert [row["tweet_id"] for row in results] == ["1"]


def test_search_matches_inside_words(client_user):
    """"bitco" must find "Bitcoin" -- a lexeme is not a prefix."""
    _track("alpha")
    upsert_tweet(_item("1", text="Buying #Bitcoin today"))
    for query in ("bitco", "itcoi", "BITCOIN"):
        results = client_user.get(f"/api/feed/?q={query}").json()["results"]
        assert [row["tweet_id"] for row in results] == ["1"], query


def test_search_matches_decoded_entities(client_user):
    """Searching "R&D" must not be defeated by X storing "R&amp;D"."""
    _track("alpha")
    upsert_tweet(_item("1", text="investing in R&amp;D this year"))
    results = client_user.get("/api/feed/?q=R%26D").json()["results"]
    assert [row["tweet_id"] for row in results] == ["1"]


# --- text_clean -------------------------------------------------------------


def test_clean_text_is_derived_and_raw_is_kept(client_user):
    """The archive keeps what X sent; the console reads the readable version."""
    _track("alpha")
    raw = "case https://t.co/dup https://t.co/dup and R&amp;D"
    upsert_tweet(_item("1", text=raw))

    row = client_user.get("/api/feed/").json()["results"][0]
    assert row["text"] == raw, "the verbatim archive must survive"
    assert row["text_clean"] == "case https://t.co/dup and R&D"


def test_duplicate_url_entities_are_presented_once(client_user):
    """X sends the same t.co twice in entities.urls; the API shows it once."""
    _track("alpha")
    entry = {"short": "https://t.co/dup", "expanded": "https://reut.rs/x"}
    upsert_tweet(_item("1", entities={"urls": [entry, entry], "hashtags": []}))
    row = client_user.get("/api/feed/").json()["results"][0]
    assert row["entities"]["urls"] == [entry]


# --- Archive scope ----------------------------------------------------------


def test_untracked_posts_are_reachable_but_not_default(client_user):
    """6,159 archived posts were counted in the totals but no screen could open them."""
    _track("alpha")
    TwitterUser.objects.update_or_create(handle="dropped", defaults={"tracking": False})
    upsert_tweet(_item("kept", account="alpha"))
    upsert_tweet(_item("orphan", account="dropped"))

    default = client_user.get("/api/feed/").json()["results"]
    assert [row["tweet_id"] for row in default] == ["kept"]

    widened = client_user.get("/api/feed/?include_untracked=1").json()["results"]
    assert {row["tweet_id"] for row in widened} == {"kept", "orphan"}


def test_ingestion_totals_separate_tracked_from_archived(client_user):
    """"Archive total" must not advertise posts the feed cannot show."""
    _track("alpha")
    TwitterUser.objects.update_or_create(handle="dropped", defaults={"tracking": False})
    upsert_tweet(_item("kept", account="alpha"))
    upsert_tweet(_item("orphan", account="dropped"))

    totals = client_user.get("/api/analytics/ingestion/?range=24h").json()["totals"]
    assert totals["archive_total"] == 2
    assert totals["archive_tracked"] == 1
    # Two stores with two retentions are reported apart as well as summed.
    assert "captured_archive" in totals and "captured_search" in totals


def test_previous_period_is_flagged_when_it_predates_the_archive(client_user):
    """"+60K vs the previous equal period" on a 13-day-old deployment was noise."""
    _track("alpha")
    upsert_tweet(_item("1"))
    totals = client_user.get("/api/analytics/ingestion/?range=90d").json()["totals"]
    assert totals["has_previous"] is False


# --- Calendar windows -------------------------------------------------------


def test_today_is_a_calendar_day_not_a_rolling_window(client_user):
    """At 00:30 Tehran, a rolling 24h labelled "Today" was mostly yesterday."""
    from fetcher.processing import TZ

    _track("alpha")
    now_local = timezone.now().astimezone(TZ)
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    today = upsert_tweet(_item("today"))
    Tweet.objects.filter(pk=today.pk).update(created_at=midnight + timedelta(minutes=5))
    stale = upsert_tweet(_item("yesterday"))
    Tweet.objects.filter(pk=stale.pk).update(created_at=midnight - timedelta(minutes=5))

    results = client_user.get("/api/feed/?window=today").json()["results"]
    assert [row["tweet_id"] for row in results] == ["today"]

    # The rolling window still exists and still includes both.
    rolling = client_user.get("/api/feed/?window=24h").json()["results"]
    assert {row["tweet_id"] for row in rolling} == {"today", "yesterday"}


# --- Run reporting ----------------------------------------------------------


def test_ingest_reports_new_rows_separately_from_seen(db):
    """A repoll that re-stored the same 40 hits claimed "40 results stored"."""
    _track("alpha")
    first = ingest_tweets([_item("1"), _item("2")], "live")
    assert (int(first), first.new) == (2, 2)

    # Same two posts again: seen by the run, new to nobody.
    again = ingest_tweets([_item("1"), _item("2")], "live")
    assert (int(again), again.new) == (2, 0)

    mixed = ingest_tweets([_item("2"), _item("3")], "live")
    assert (int(mixed), mixed.new) == (2, 1)


# --- Accounts roster --------------------------------------------------------


def test_accounts_returns_the_roster_not_every_author_seen(client_user):
    """2,409 rows and 7,230 buttons shipped on every page load; 64 were tracked."""
    _track("alpha")
    for n in range(5):
        TwitterUser.objects.create(handle=f"seen{n}", tracking=False)

    roster = client_user.get("/api/accounts/").json()
    assert [row["handle"] for row in roster] == ["alpha"]

    # The rest are reachable by search, which is how you start tracking one.
    found = client_user.get("/api/accounts/?q=seen3").json()
    assert [row["handle"] for row in found] == ["seen3"]

    everything = client_user.get("/api/accounts/?tracking=all").json()
    assert len(everything) == 6


# --- Export -----------------------------------------------------------------


def test_export_carries_every_metric_and_offers_raw_text(client_user):
    """The export silently dropped replies, quotes and bookmarks."""
    _track("alpha")
    raw = "R&amp;D https://t.co/dup https://t.co/dup"
    upsert_tweet(_item("1", text=raw, replies=3, quotes=2, bookmarks=9))

    body = b"".join(
        client_user.get("/api/export/?format=csv").streaming_content
    ).decode()
    header, row = body.splitlines()[0], body.splitlines()[1]
    for column in ("replies", "quotes", "bookmarks"):
        assert column in header
    assert "R&D" in row and "R&amp;D" not in row

    verbatim = b"".join(
        client_user.get("/api/export/?format=csv&text=raw").streaming_content
    ).decode()
    assert "R&amp;D" in verbatim
