"""Media archiving as a queue rather than a repeated search for work.

The archiver used to stat every MediaAsset file and then full-scan the tweet
table -- loading the JSONB extras column -- every 120 seconds, to download at
most a handful of files. It restarted from the top each tick, so once the front
of the archive was complete it re-walked all of it before reaching anything new.

What matters now is that the work is recorded at ingest, that a tick costs the
same regardless of archive size, and that a URL which can never succeed stops
being retried.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from fetching.ingest import upsert_tweet
from fetching.media import MAX_ATTEMPTS, archive_batch, relative_path_for
from tweets.models import MediaAsset, PendingMedia, Tweet, TwitterUser

PHOTO = "https://pbs.twimg.com/media/a.jpg"


class _Resp(BytesIO):
    headers = {"Content-Length": "4"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok():
    return patch("fetching.media.urlopen", return_value=_Resp(b"jpeg"))


def _fails():
    return patch("fetching.media.urlopen", side_effect=OSError("gone"))


def _tweet(rest_id: str, url: str = PHOTO, account: str = "jack"):
    TwitterUser.objects.update_or_create(handle=account, defaults={"tracking": True})
    return upsert_tweet(
        {
            "rest_id": rest_id,
            "author_id": "1",
            "account": account,
            "text": "pic",
            "created_at": "Wed Oct 10 20:19:24 +0000 2018",
            "media": [{"type": "photo", "url": url}],
        }
    )


@pytest.mark.django_db
def test_ingest_records_the_work_instead_of_leaving_it_to_be_found():
    _tweet("1")
    assert PendingMedia.objects.filter(remote_url=PHOTO).exists()


@pytest.mark.django_db
def test_reingesting_a_tweet_does_not_duplicate_its_queue_entry():
    """Live re-sees the same posts constantly, so this has to be idempotent."""
    _tweet("1")
    _tweet("1")
    _tweet("1")
    assert PendingMedia.objects.filter(remote_url=PHOTO).count() == 1


@pytest.mark.django_db
def test_only_twimg_urls_are_queued():
    """The host allowlist is the SSRF ceiling and applies before the download."""
    _tweet("1", url="https://evil.example.com/a.jpg")
    assert not PendingMedia.objects.exists()


@pytest.mark.django_db
def test_a_stored_file_leaves_the_queue(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    _tweet("1")
    with _ok():
        assert archive_batch(25) == 1
    assert MediaAsset.objects.filter(remote_url=PHOTO).exists()
    # Otherwise every later tick would try it again.
    assert not PendingMedia.objects.filter(remote_url=PHOTO).exists()


@pytest.mark.django_db
def test_a_dead_url_is_abandoned_after_max_attempts(settings, tmp_path):
    """Media disappears from X routinely. Without a ceiling those rows sit at
    the head of the queue being retried every 120 seconds forever, which is the
    failure the queue exists to end.
    """
    settings.MEDIA_ROOT = tmp_path
    _tweet("1")

    for _ in range(MAX_ATTEMPTS):
        with _fails():
            assert archive_batch(25) == 0

    row = PendingMedia.objects.get(remote_url=PHOTO)
    assert row.attempts == MAX_ATTEMPTS
    # Kept, not deleted: the reason stays inspectable, and a re-enqueue does not
    # silently resurrect it.
    assert row.last_error

    with patch("fetching.media.urlopen", side_effect=AssertionError("must not retry")):
        assert archive_batch(25) == 0


@pytest.mark.django_db
def test_a_url_two_tweets_share_is_downloaded_once(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    _tweet("1")
    _tweet("2")
    assert PendingMedia.objects.count() == 1

    with _ok():
        assert archive_batch(25) == 1


@pytest.mark.django_db
def test_a_queue_row_for_something_already_archived_is_dropped_without_a_fetch(
    settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    relative = relative_path_for(PHOTO)
    (Path(tmp_path) / relative).parent.mkdir(parents=True, exist_ok=True)
    (Path(tmp_path) / relative).write_bytes(b"jpeg")
    MediaAsset.objects.create(remote_url=PHOTO, relative_path=relative)
    PendingMedia.objects.create(remote_url=PHOTO)

    with patch("fetching.media.urlopen", side_effect=AssertionError("must not fetch")):
        assert archive_batch(25) == 0
    assert not PendingMedia.objects.filter(remote_url=PHOTO).exists()


@pytest.mark.django_db
def test_a_tick_does_not_scan_the_tweet_table(settings, tmp_path, django_assert_max_num_queries):
    """The whole point: cost per tick is bounded by the batch size, not by how
    much archive there is behind it.
    """
    settings.MEDIA_ROOT = tmp_path
    for index in range(25):
        _tweet(str(index), url=f"https://pbs.twimg.com/media/{index}.jpg")
    assert Tweet.objects.count() == 25

    with _ok():
        with django_assert_max_num_queries(20):
            archive_batch(2)


@pytest.mark.django_db
def test_batch_limit_is_respected(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    for index in range(5):
        _tweet(str(index), url=f"https://pbs.twimg.com/media/{index}.jpg")

    with _ok():
        assert archive_batch(2) == 2
    assert PendingMedia.objects.count() == 3


@pytest.mark.django_db
def test_seed_command_queues_tweets_that_predate_the_queue():
    """Rows ingested before the queue existed have no entry, and without the
    backfill they would simply never be archived.
    """
    _tweet("1")
    PendingMedia.objects.all().delete()  # as if ingested before the queue

    call_command("seed_media_queue")
    assert PendingMedia.objects.filter(remote_url=PHOTO).exists()


@pytest.mark.django_db
def test_seed_command_is_idempotent_and_supports_dry_run():
    _tweet("1")
    PendingMedia.objects.all().delete()

    call_command("seed_media_queue", "--dry-run")
    assert not PendingMedia.objects.exists()

    call_command("seed_media_queue")
    call_command("seed_media_queue")
    assert PendingMedia.objects.count() == 1


@pytest.mark.django_db
def test_seed_command_skips_what_is_already_archived():
    _tweet("1")
    PendingMedia.objects.all().delete()
    MediaAsset.objects.create(remote_url=PHOTO, relative_path=relative_path_for(PHOTO))

    call_command("seed_media_queue")
    assert not PendingMedia.objects.exists()
