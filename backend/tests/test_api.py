"""Integration tests: feed scope/order and the search lifecycle, through DRF."""
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from rest_framework.test import APIClient

from fetching.ingest import ingest_search_hits, upsert_tweet
from tweets.models import (
    FetchRun,
    Search,
    SearchHit,
    SearchTweet,
    Tweet,
    TwitterUser,
    XSession,
)


@pytest.fixture
def client_user(db):
    """An operator: staff, because this suite exercises the operator console.

    Signup is open and ordinary users are read-only, so the endpoints below that
    retier accounts, replace the X session, or start a cycle all require staff.
    The read-only side of that boundary is covered in test_auth_api.py.
    """
    user = User.objects.create_user(username="alice", password="pw", is_staff=True)
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


@pytest.fixture
def client_reader(db):
    """A plain signed-in account, as an open signup produces."""
    user = User.objects.create_user(username="reader", password="pw")
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def _tweet(account, rest_id, created):
    return upsert_tweet(
        {
            "rest_id": rest_id,
            "author_id": "1",
            "account": account,
            "text": f"t{rest_id}",
            "created_at": created,
        }
    )


@pytest.mark.django_db
def test_feed_serves_tracked_accounts_newest_first(client_user):
    client, user = client_user
    TwitterUser.objects.create(handle="jack", tracking=True, priority=1)

    _tweet("jack", "1", "Wed Oct 10 20:19:24 +0000 2018")
    _tweet("jack", "2", "Wed Oct 10 21:19:24 +0000 2018")
    _tweet("someoneelse", "3", "Wed Oct 10 22:19:24 +0000 2018")

    resp = client.get("/api/feed/")
    assert resp.status_code == 200
    # Untracked "3" is absent: the feed is the UserTweets collector's output.
    assert [t["tweet_id"] for t in resp.data["results"]] == ["2", "1"]


@pytest.mark.django_db
def test_feed_excludes_search_results(client_user):
    """The feed is one collector, not a blend of two.

    Saved searches used to be unioned into the feed, so merely saving a query
    rewrote what every user saw, and a search hit inherited the archive's
    retention. Hits now live in their own table, reachable only through the
    search that found them.
    """
    client, _ = client_user
    TwitterUser.objects.create(handle="jack", tracking=True, priority=1)
    _tweet("jack", "9", "Wed Oct 10 20:19:24 +0000 2018")
    search = Search.objects.create(name="s", slug="s", raw_query="s", enabled=True)
    ingest_search_hits(
        search,
        [{
            "rest_id": "77",
            "author_id": "5",
            "account": "stranger",
            "text": "found by search",
            "created_at": "Wed Oct 10 23:19:24 +0000 2018",
        }],
    )

    feed_ids = [x["tweet_id"] for x in client.get("/api/feed/").data["results"]]
    result_ids = [
        x["tweet_id"]
        for x in client.get(f"/api/searches/{search.id}/results/").data["results"]
    ]

    assert feed_ids == ["9"]
    assert result_ids == ["77"]


@pytest.mark.django_db
def test_feed_requires_auth():
    assert APIClient().get("/api/feed/").status_code == 401


@pytest.mark.django_db
def test_account_timeline_filters_by_handle(client_user):
    client, _ = client_user
    _tweet("jack", "1", "Wed Oct 10 20:19:24 +0000 2018")
    _tweet("elon", "2", "Wed Oct 10 20:19:24 +0000 2018")
    resp = client.get("/api/accounts/jack/tweets/")
    assert [t["tweet_id"] for t in resp.data["results"]] == ["1"]


@pytest.mark.django_db
def test_tweet_api_exposes_rich_author_media_and_nested_content(client_user):
    client, _user = client_user
    TwitterUser.objects.create(
        handle="jack",
        display_name="Jack",
        avatar_url="https://img.example/jack.jpg",
        verified=True,
        tracking=True,
        priority=1,
    )
    upsert_tweet({
        "rest_id": "rich",
        "author_id": "42",
        "account": "jack",
        "author": {
            "id": "42",
            "handle": "jack",
            "display_name": "Jack",
            "avatar_url": "https://img.example/jack.jpg",
            "verified": True,
        },
        "text": "rich tweet",
        "media": [{"type": "photo", "url": "https://img.example/photo.jpg"}],
        "quoted_tweet": {"id": "quoted", "text": "quoted text"},
        "bookmarks": 7,
    })

    data = client.get("/api/feed/").data["results"][0]

    assert data["author"]["display_name"] == "Jack"
    assert data["author"]["verified"] is True
    assert data["author"]["avatar_url"] == "https://img.example/jack.jpg"
    assert data["media"][0]["type"] == "photo"
    assert data["quoted_tweet"]["id"] == "quoted"
    assert data["bookmarks"] == 7


@pytest.mark.django_db
def test_recent_fetch_runs_expose_status_and_failure_ledger(client_user):
    client, _ = client_user
    FetchRun.objects.create(
        run_id="run-visible",
        subsystem="search",
        target="oil:Latest",
        status="partial",
        failure_ledger={"SearchTimeline:404": {"count": 2}},
    )

    data = client.get("/api/runs/?subsystem=search").data["results"][0]

    assert data["run_id"] == "run-visible"
    assert data["status"] == "partial"
    assert data["failure_ledger"]["SearchTimeline:404"]["count"] == 2


@pytest.mark.django_db
def test_create_search_enqueues_and_subscribes(client_user):
    client, user = client_user
    with patch("fetching.tasks.run_search.delay") as delay:
        resp = client.post(
            "/api/searches/", {"raw_query": "openai", "product": "Latest"}, format="json"
        )
    assert resp.status_code == 201
    search = Search.objects.get(raw_query="openai")
    assert search.slug
    delay.assert_called_once_with(search.id)


@pytest.mark.django_db
def test_search_list_filters_by_product(client_user):
    client, user = client_user
    Search.objects.create(name="a", slug="a", raw_query="a", product="Top")
    Search.objects.create(name="b", slug="b", raw_query="b", product="Latest")
    resp = client.get("/api/searches/?product=Latest")
    assert [s["raw_query"] for s in resp.data["results"]] == ["b"]


@pytest.mark.django_db
def test_search_list_is_operator_wide(client_user):
    client, user = client_user
    other = User.objects.create_user(username="eve", password="pw")
    Search.objects.create(name="mine", slug="mine", raw_query="mine")
    Search.objects.create(name="eves", slug="eves", raw_query="eves")
    resp = client.get("/api/searches/")
    assert {s["raw_query"] for s in resp.data["results"]} == {"mine", "eves"}


@pytest.mark.django_db
def test_search_create_derives_name_from_raw_query(client_user):
    client, _ = client_user
    with patch("fetching.tasks.run_search.delay"):
        resp = client.post(
            "/api/searches/", {"raw_query": "openai", "product": "Latest"}, format="json"
        )
    assert resp.status_code == 201
    search = Search.objects.get(raw_query="openai")
    assert search.name == "openai"  # derived from raw_query when name omitted


@pytest.mark.django_db
def test_session_api_returns_names_only_and_updates_active_session(client_user):
    client, _ = client_user
    FetchRun.objects.create(run_id="auth-needed", subsystem="live", status="auth_required")

    updated = client.post(
        "/api/session/",
        {"cookies": {"auth_token": "never-return-this"}, "headers": {"authorization": "Bearer secret"}},
        format="json",
    )
    health = client.get("/api/session/")

    assert updated.status_code == 200
    assert health.data["configured"] is True
    assert health.data["cookie_names"] == ["auth_token"]
    assert health.data["header_names"] == ["authorization"]
    assert "never-return-this" not in str(health.data)
    assert XSession.objects.get(name="default").cookies["auth_token"] == "never-return-this"


@pytest.mark.django_db
def test_session_api_rejects_non_object_credentials(client_user):
    client, _ = client_user

    response = client.post("/api/session/", {"cookies": [], "headers": {}}, format="json")

    assert response.status_code == 400


@pytest.mark.django_db
def test_backfill_tweet_payloads_refreshes_author_from_stored_payload():
    """Sparse rows gain author fields by re-upserting Tweet.payload (no X calls)."""
    tweet = Tweet.objects.create(
        dedup_key="42:legacy",
        tweet_id="legacy",
        author_rest_id="42",
        account="jack",
        text="old",
        payload={
            "rest_id": "legacy",
            "author_id": "42",
            "account": "jack",
            "author": {
                "id": "42",
                "handle": "jack",
                "display_name": "Jack Dorsey",
                "avatar_url": "https://img.example/jack.jpg",
                "verified": True,
                "verified_type": "Blue",
            },
            "text": "old",
            "bookmarks": 3,
            "media": [{"type": "photo", "url": "https://img.example/p.jpg"}],
        },
    )
    assert tweet.author_id is None

    call_command("backfill_tweet_payloads")

    tweet.refresh_from_db()
    assert tweet.author is not None
    assert tweet.author.display_name == "Jack Dorsey"
    assert tweet.author.avatar_url == "https://img.example/jack.jpg"
    assert tweet.author.verified is True
    assert tweet.bookmarks == 3
    assert tweet.payload["media"][0]["type"] == "photo"


@pytest.mark.django_db
def test_feed_filters_by_text_query(client_user):
    client, _ = client_user
    TwitterUser.objects.create(handle="jack", tracking=True, priority=1)
    upsert_tweet(
        {
            "rest_id": "1",
            "author_id": "1",
            "account": "jack",
            "text": "openai ships gpt",
            "created_at": "Wed Oct 10 20:19:24 +0000 2018",
        }
    )
    upsert_tweet(
        {
            "rest_id": "2",
            "author_id": "1",
            "account": "jack",
            "text": "unrelated weather",
            "created_at": "Wed Oct 10 21:19:24 +0000 2018",
        }
    )
    ids = [t["tweet_id"] for t in client.get("/api/feed/?q=openai").data["results"]]
    assert ids == ["1"]


@pytest.mark.django_db
def test_export_jsonl_shares_feed_filters(client_user):
    client, _ = client_user
    TwitterUser.objects.create(handle="jack", tracking=True, priority=1)
    upsert_tweet(
        {
            "rest_id": "1",
            "author_id": "1",
            "account": "jack",
            "text": "hello",
            "created_at": "Wed Oct 10 20:19:24 +0000 2018",
        }
    )
    resp = client.get("/api/export/?format=jsonl")
    assert resp.status_code == 200
    body = b"".join(resp.streaming_content).decode()
    lines = [line for line in body.splitlines() if line]
    assert json.loads(lines[0])["tweet_id"] == "1"


@pytest.mark.django_db
def test_feed_filters_by_tier(client_user):
    client, _ = client_user
    TwitterUser.objects.create(handle="jack", tracking=True, priority=1)
    TwitterUser.objects.create(handle="elon", tracking=True, priority=7)
    upsert_tweet(
        {
            "rest_id": "1",
            "author_id": "1",
            "account": "jack",
            "text": "a",
            "created_at": "Wed Oct 10 20:19:24 +0000 2018",
            "source_endpoint": "UserTweets",
        }
    )
    upsert_tweet(
        {
            "rest_id": "2",
            "author_id": "2",
            "account": "elon",
            "text": "b",
            "created_at": "Wed Oct 10 21:19:24 +0000 2018",
            "source_endpoint": "UserTweetsAndReplies",
        }
    )
    assert [t["tweet_id"] for t in client.get("/api/feed/?tier=1").data["results"]] == ["1"]


@pytest.mark.django_db
def test_accounts_list_exposes_priority_and_policy(client_user):
    from fetching.accounts import policy_for

    client, _ = client_user
    TwitterUser.objects.create(handle="jack", tracking=True, priority=1, display_name="Jack")
    data = client.get("/api/accounts/").data
    rows = data if isinstance(data, list) else data["results"]
    assert rows[0]["handle"] == "jack"
    assert rows[0]["priority"] == 1
    # An unmeasured account reports its tier default. Asserted against the policy
    # rather than a literal, so re-tiering does not need a test edit to match.
    assert rows[0]["poll_interval_seconds"] == policy_for(1)["poll_interval_seconds"]


@pytest.mark.django_db
def test_accounts_list_reports_the_measured_cadence_when_there_is_one(client_user):
    client, _ = client_user
    TwitterUser.objects.create(
        handle="jack", tracking=True, priority=1,
        poll_interval_seconds=2400, observed_median_gap_seconds=2400,
    )
    data = client.get("/api/accounts/").data
    rows = data if isinstance(data, list) else data["results"]
    assert rows[0]["poll_interval_seconds"] == 2400
    assert rows[0]["observed_median_gap_seconds"] == 2400


@pytest.mark.django_db
def test_account_delete_is_disabled(client_user):
    client, _ = client_user
    TwitterUser.objects.create(handle="jack", tracking=True)
    assert client.delete("/api/accounts/jack/").status_code == 405
    assert TwitterUser.objects.filter(handle="jack").exists()


@pytest.mark.django_db
def test_account_create_tracks_and_enqueues(client_user):
    client, _ = client_user
    with patch("fetching.tasks.fetch_account_historical.delay") as hist, patch(
        "fetching.tasks.fetch_account_live.delay"
    ) as live:
        resp = client.post("/api/accounts/", {"handle": "@Jack", "priority": 2}, format="json")
    assert resp.status_code == 201
    acct = TwitterUser.objects.get(handle="jack")
    assert acct.tracking is True and acct.priority == 2
    hist.assert_called_once_with("jack")
    live.assert_called_once_with("jack")


@pytest.mark.django_db
def test_account_unquarantine_and_fetch(client_user):
    client, _ = client_user
    TwitterUser.objects.create(
        handle="jack", tracking=True, priority=1, quarantined=True, quarantine_reason="id fail"
    )
    resp = client.patch("/api/accounts/jack/", {"quarantined": False}, format="json")
    assert resp.status_code == 200
    acct = TwitterUser.objects.get(handle="jack")
    assert acct.quarantined is False
    with patch("fetching.tasks.fetch_account_historical.delay") as hist, patch(
        "fetching.tasks.fetch_account_live.delay"
    ) as live:
        queued = client.post("/api/accounts/jack/fetch/")
    assert queued.status_code == 202
    hist.assert_called_once_with("jack")
    live.assert_called_once_with("jack")


@pytest.mark.django_db
def test_cycle_trigger_queues_global_task(client_user):
    client, _ = client_user
    with patch("fetching.tasks.poll_live_all.delay") as delay:
        resp = client.post("/api/cycles/", {"subsystem": "live"}, format="json")
    assert resp.status_code == 202
    delay.assert_called_once_with()


@pytest.mark.django_db
def test_fetch_run_detail_includes_log_excerpt(client_user):
    client, _ = client_user
    FetchRun.objects.create(
        run_id="run-detail",
        subsystem="live",
        target="all",
        status="completed",
        log_excerpt="page 1 http",
    )
    data = client.get("/api/runs/run-detail/").data
    assert data["log_excerpt"] == "page 1 http"


@pytest.mark.django_db
def test_undated_tweet_is_not_fabricated_and_does_not_stay_pinned(client_user):
    """An unparseable X timestamp must not be fabricated into storage or hidden.

    created_at stays NULL and the raw string is kept; the feed orders on
    Coalesce(created_at, ingested_at), so an undated tweet is placed at the time
    we actually saw it. The old behaviour stored a fabricated now(), which pinned
    it above real content permanently -- here it is overtaken by the next arrival.
    """
    from datetime import timedelta

    from django.utils import timezone

    client, _ = client_user
    TwitterUser.objects.create(handle="jack", tracking=True, priority=1)

    undated = upsert_tweet(
        {"rest_id": "undated", "author_id": "1", "account": "jack",
         "text": "no date", "created_at": "not-a-real-timestamp"}
    )
    assert undated.created_at is None, "unparseable timestamps must not be fabricated"
    assert undated.raw_created_at == "not-a-real-timestamp", "original string is kept"

    # Pin arrival to two hours ago so the ordering is decided by the data, not by
    # sub-second timing between the two inserts.
    seen_at = timezone.now() - timedelta(hours=2)
    Tweet.objects.filter(pk=undated.pk).update(ingested_at=seen_at)

    # A tweet genuinely posted after we saw the undated one must outrank it.
    _tweet("jack", "newer", timezone.now().strftime("%a %b %d %H:%M:%S +0000 %Y"))

    ids = [t["tweet_id"] for t in client.get("/api/feed/").data["results"]]
    assert "undated" in ids, "an undated tweet must not vanish from the feed"
    assert ids[0] == "newer", "an undated tweet must not stay pinned above newer content"


# --- Operator boundary ------------------------------------------------------


@pytest.mark.django_db
def test_a_reader_can_browse_but_not_change_the_account_roster(client_reader):
    client, _ = client_reader
    TwitterUser.objects.create(handle="jack", tracking=True, priority=3)

    assert client.get("/api/accounts/").status_code == 200
    assert client.patch("/api/accounts/jack/", {"priority": 1}, format="json").status_code == 403
    assert client.post("/api/accounts/", {"handle": "new"}, format="json").status_code == 403
    assert client.post("/api/accounts/jack/fetch/").status_code == 403
    assert TwitterUser.objects.get(handle="jack").priority == 3


@pytest.mark.django_db
def test_a_reader_can_browse_but_not_create_searches(client_reader):
    client, _ = client_reader
    Search.objects.create(name="ai", slug="ai", raw_query="ai", enabled=True)

    assert client.get("/api/searches/").status_code == 200
    assert client.post("/api/searches/", {"raw_query": "new"}, format="json").status_code == 403
    assert Search.objects.count() == 1


def _post(client_handle, tweet_id, **over):
    """One tracked-account tweet, through the real ingest path."""
    item = {
        "rest_id": tweet_id,
        "author_id": tweet_id,
        "account": client_handle,
        "text": f"post {tweet_id}",
        "created_at": "Wed Oct 10 20:19:24 +0000 2018",
        "source_endpoint": "UserTweets",
    }
    item.update(over)
    return upsert_tweet(item)


@pytest.mark.django_db
def test_feed_sorted_by_engagement_ranks_by_the_shared_formula(client_user):
    client, _ = client_user
    TwitterUser.objects.create(handle="jack", tracking=True)
    _post("jack", "quiet", likes=1, retweets=0, views=0)
    _post("jack", "loud", likes=10, retweets=100, views=1000)
    _post("jack", "middling", likes=50, retweets=1, views=2)

    body = client.get("/api/feed/?sort=top").data

    assert [t["tweet_id"] for t in body["results"]] == ["loud", "middling", "quiet"]
    # `top` cannot use a cursor (engagement is neither unique nor monotonic), so
    # it pages by offset -- which still reports `next` for InfiniteSentinel.
    assert "count" in body


@pytest.mark.django_db
def test_feed_latest_still_pages_by_cursor(client_user):
    client, _ = client_user
    TwitterUser.objects.create(handle="jack", tracking=True)
    _post("jack", "1")

    body = client.get("/api/feed/").data

    assert "count" not in body  # cursor pagination, not offset
    assert [t["tweet_id"] for t in body["results"]] == ["1"]


@pytest.mark.django_db
def test_feed_filters_by_post_type(client_user):
    client, _ = client_user
    TwitterUser.objects.create(handle="jack", tracking=True)
    _post("jack", "plain", type="Tweet")
    _post("jack", "answer", type="Reply")
    _post("jack", "boost", type="Retweet")

    ids = {t["tweet_id"] for t in client.get("/api/feed/?types=reply,retweet").data["results"]}

    assert ids == {"answer", "boost"}


@pytest.mark.django_db
def test_feed_media_only_excludes_text_posts(client_user):
    client, _ = client_user
    TwitterUser.objects.create(handle="jack", tracking=True)
    _post("jack", "text")
    _post("jack", "photo", media=[{"type": "photo", "url": "https://pbs.twimg.com/a.jpg"}])

    ids = [t["tweet_id"] for t in client.get("/api/feed/?has_media=1").data["results"]]

    assert ids == ["photo"]


@pytest.mark.django_db
def test_feed_window_bounds_by_posted_time(client_user):
    from django.utils import timezone as dj_timezone

    client, _ = client_user
    TwitterUser.objects.create(handle="jack", tracking=True)
    _post("jack", "old")
    recent = _post("jack", "new")
    Tweet.objects.filter(pk=recent.pk).update(
        created_at=dj_timezone.now() - timedelta(minutes=10)
    )

    ids = [t["tweet_id"] for t in client.get("/api/feed/?window=1h").data["results"]]

    assert ids == ["new"]


@pytest.mark.django_db
def test_search_results_are_labelled_as_search_captures(client_user):
    """A hit reports which collector found it, so the console can colour it."""
    client, _ = client_user
    search = Search.objects.create(name="ai", slug="ai", raw_query="ai", enabled=True)
    ingest_search_hits(
        search,
        [{"rest_id": "found", "author_id": "3", "account": "stranger", "text": "hit"}],
    )

    row = client.get(f"/api/searches/{search.id}/results/").data["results"][0]

    assert row["source_subsystem"] == "search"
    assert row["source_endpoint"] == "SearchTimeline"


@pytest.mark.django_db
def test_feed_filters_by_several_accounts_at_once(client_user):
    """Repeatable ?account=, the same spelling the analytics endpoints take.

    params.get() keeps only the last value, so a two-account selection used to
    come back as one account's posts.
    """
    client, _ = client_user
    for handle in ("jack", "elon", "paul"):
        TwitterUser.objects.create(handle=handle, tracking=True)
        _post(handle, handle)

    body = client.get("/api/feed/?account=jack&account=elon").data

    assert sorted(t["tweet_id"] for t in body["results"]) == ["elon", "jack"]


@pytest.mark.django_db
def test_feed_accepts_a_comma_joined_account_list(client_user):
    client, _ = client_user
    for handle in ("jack", "elon"):
        TwitterUser.objects.create(handle=handle, tracking=True)
        _post(handle, handle)

    body = client.get("/api/feed/?account=@Jack,elon").data

    assert sorted(t["tweet_id"] for t in body["results"]) == ["elon", "jack"]
