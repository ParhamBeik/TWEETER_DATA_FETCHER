"""The 0016 data migration, exercised against rows that already exist.

The rest of the suite starts from a fully migrated, empty database, so it can
only prove the schema applies -- not that the copy from SearchResult into
SearchTweet/SearchHit actually moves anything. That copy runs exactly once, on a
production database holding the only copy of the data, which is the worst
possible place to find out it was wrong.

Driven through MigrationExecutor rather than the ORM: the historical models are
the whole point, and importing tweets.models here would test today's schema
against yesterday's data.
"""
import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = ("tweets", "0015_mediaasset")
AFTER = ("tweets", "0016_search_domain_split")


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    executor.loader.build_graph()
    return executor.loader.project_state(targets).apps


@pytest.fixture
def at_0015(django_db_setup, django_db_blocker):
    """Rewind to just before the split, and always roll forward again.

    Leaving the connection on an old schema would break every test that runs
    after this module in the same session.
    """
    with django_db_blocker.unblock():
        yield _migrate([BEFORE])
        _migrate([AFTER])


def _tweet(apps, rest_id, account, **over):
    return apps.get_model("tweets", "Tweet").objects.create(
        dedup_key=f"9:{rest_id}",
        tweet_id=rest_id,
        author_rest_id="9",
        account=account,
        text=f"post {rest_id}",
        likes=3,
        views=100,
        entities={"hashtags": ["gold"]},
        extras={"media": []},
        payload={"rest_id": rest_id},
        **over,
    )


@pytest.mark.django_db(transaction=True)
def test_existing_search_results_are_copied_into_their_own_tables(at_0015):
    old = at_0015
    search = old.get_model("tweets", "Search").objects.create(
        name="gold", slug="gold", raw_query="gold", product="Top"
    )
    linked = _tweet(old, "1", "stranger")
    _tweet(old, "2", "stranger")  # not a search result; must not be copied
    old.get_model("tweets", "SearchResult").objects.create(
        search=search, tweet=linked, rank=4
    )

    new = _migrate([AFTER])

    hits = new.get_model("tweets", "SearchHit").objects.all()
    bodies = new.get_model("tweets", "SearchTweet").objects.all()
    assert bodies.count() == 1
    assert bodies.get().tweet_id == "1"
    # The payload has to survive, not just the id -- these rows are what the
    # search page renders.
    assert bodies.get().text == "post 1"
    assert bodies.get().entities == {"hashtags": ["gold"]}
    assert hits.count() == 1
    assert hits.get().rank == 4
    assert hits.get().search_id == search.id


@pytest.mark.django_db(transaction=True)
def test_the_copy_leaves_the_original_rows_alone(at_0015):
    """Non-destructive by design; purge_orphan_search_tweets does the deleting.

    A migration that silently deletes rows gives an operator no chance to look
    at what it would remove first.
    """
    old = at_0015
    search = old.get_model("tweets", "Search").objects.create(
        name="gold", slug="gold", raw_query="gold", product="Top"
    )
    tweet = _tweet(old, "1", "stranger")
    old.get_model("tweets", "SearchResult").objects.create(search=search, tweet=tweet, rank=0)

    new = _migrate([AFTER])

    assert new.get_model("tweets", "Tweet").objects.filter(tweet_id="1").exists()


@pytest.mark.django_db(transaction=True)
def test_one_tweet_matched_by_two_searches_is_copied_once(at_0015):
    old = at_0015
    Search = old.get_model("tweets", "Search")
    gold = Search.objects.create(name="gold", slug="gold", raw_query="gold", product="Top")
    oil = Search.objects.create(name="oil", slug="oil", raw_query="oil", product="Top")
    tweet = _tweet(old, "1", "stranger")
    SearchResult = old.get_model("tweets", "SearchResult")
    SearchResult.objects.create(search=gold, tweet=tweet, rank=0)
    SearchResult.objects.create(search=oil, tweet=tweet, rank=1)

    new = _migrate([AFTER])

    assert new.get_model("tweets", "SearchTweet").objects.count() == 1
    assert new.get_model("tweets", "SearchHit").objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_an_archive_with_no_searches_migrates_cleanly(at_0015):
    _tweet(at_0015, "1", "jack")

    new = _migrate([AFTER])

    assert new.get_model("tweets", "SearchTweet").objects.count() == 0
    assert new.get_model("tweets", "Tweet").objects.count() == 1
