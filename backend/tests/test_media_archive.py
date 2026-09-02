"""Photo archive: URL rewrite, host allowlist, skip-if-present download.

Unit tests for the pure rewrite/allowlist (no I/O). One django_db test for the
serializer preferring a local path, and one for archive_batch skipping a file
that is already on disk -- both sit at the ORM/disk boundary, not the network.
"""
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from fetching.ingest import upsert_tweet
from fetching.media import (
    archive_batch,
    is_allowed_photo_url,
    lookup_local_urls,
    photo_urls_from_extras,
    relative_path_for,
    rewrite_media_blob,
)
from tweets.models import MediaAsset, TwitterUser


def test_only_https_twimg_hosts_are_allowed():
    assert is_allowed_photo_url("https://pbs.twimg.com/media/a.jpg")
    assert is_allowed_photo_url("https://abs.twimg.com/sticky/a.png")
    assert not is_allowed_photo_url("http://pbs.twimg.com/media/a.jpg")
    assert not is_allowed_photo_url("https://evil.example/pbs.twimg.com/a.jpg")
    assert not is_allowed_photo_url("https://pbs.twimg.com.evil/a.jpg")


def test_photo_urls_walk_quoted_and_reposted_blobs():
    """Photos from every nesting level, plus a video's poster frame.

    Video used to be skipped here on the theory that it could play from X's CDN.
    It cannot -- X answers those requests with 403 -- so the poster and the
    chosen mp4 variant are both archived now.
    """
    extras = {
        "media": [
            {"type": "photo", "url": "https://pbs.twimg.com/a.jpg"},
            {"type": "video", "url": "https://pbs.twimg.com/poster.jpg"},
        ],
        "quoted_tweet": {"media": [{"type": "photo", "url": "https://pbs.twimg.com/q.jpg"}]},
        "retweeted_tweet": {"media": [{"type": "photo", "url": "https://pbs.twimg.com/r.jpg"}]},
    }
    assert photo_urls_from_extras(extras) == [
        "https://pbs.twimg.com/a.jpg",
        "https://pbs.twimg.com/poster.jpg",
        "https://pbs.twimg.com/q.jpg",
        "https://pbs.twimg.com/r.jpg",
    ]


def test_video_variant_is_archived_and_played_locally():
    """A video with mp4 variants yields a playable local `src`."""
    item = {
        "type": "video",
        "url": "https://pbs.twimg.com/poster.jpg",
        "variants": [
            {"url": "https://video.twimg.com/x.m3u8", "content_type": "application/x-mpegURL"},
            {"url": "https://video.twimg.com/mid.mp4", "content_type": "video/mp4", "bitrate": 832000},
            {"url": "https://video.twimg.com/max.mp4", "content_type": "video/mp4", "bitrate": 9000000},
        ],
    }
    urls = photo_urls_from_extras({"media": [item]})
    assert "https://video.twimg.com/mid.mp4" in urls
    # The 9Mbps master is deliberately not archived.
    assert "https://video.twimg.com/max.mp4" not in urls

    out = rewrite_media_blob(
        {"media": [item]},
        {
            "https://video.twimg.com/mid.mp4": "/media/ab/cd.mp4",
            "https://pbs.twimg.com/poster.jpg": "/media/ef/gh.jpg",
        },
    )
    assert out["media"][0]["src"] == "/media/ab/cd.mp4"
    assert out["media"][0]["url"] == "/media/ef/gh.jpg"


def test_rewrite_swaps_only_photos_we_have():
    extras = {
        "media": [
            {"type": "photo", "url": "https://pbs.twimg.com/a.jpg"},
            {"type": "video", "url": "https://pbs.twimg.com/v.mp4"},
        ]
    }
    out = rewrite_media_blob(extras, {"https://pbs.twimg.com/a.jpg": "/media/aa/hash.jpg"})
    assert out["media"][0]["url"] == "/media/aa/hash.jpg"
    assert out["media"][1]["url"] == "https://pbs.twimg.com/v.mp4"
    assert extras["media"][0]["url"] == "https://pbs.twimg.com/a.jpg"


@pytest.mark.django_db
def test_feed_prefers_a_local_copy_when_the_file_exists(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.MEDIA_URL = "/media/"
    TwitterUser.objects.create(handle="jack", tracking=True)
    remote = "https://pbs.twimg.com/media/a.jpg"
    relative = relative_path_for(remote)
    dest = Path(tmp_path) / relative
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"jpeg")
    MediaAsset.objects.create(remote_url=remote, relative_path=relative)
    upsert_tweet({
        "rest_id": "1", "author_id": "1", "account": "jack",
        "text": "pic", "created_at": "Wed Oct 10 20:19:24 +0000 2018",
        "media": [{"type": "photo", "url": remote}],
    })
    user = User.objects.create_user(username="alice", password="pw", is_staff=True)
    client = APIClient()
    client.force_authenticate(user=user)
    row = client.get("/api/feed/").data["results"][0]
    assert row["media"][0]["url"] == f"/media/{relative}"


@pytest.mark.django_db
def test_archive_batch_does_not_refetch_a_file_already_on_disk(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    remote = "https://pbs.twimg.com/media/a.jpg"
    relative = relative_path_for(remote)
    dest = Path(tmp_path) / relative
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"jpeg")
    MediaAsset.objects.create(remote_url=remote, relative_path=relative)
    TwitterUser.objects.create(handle="jack", tracking=True)
    upsert_tweet({
        "rest_id": "1", "author_id": "1", "account": "jack",
        "text": "pic", "created_at": "Wed Oct 10 20:19:24 +0000 2018",
        "media": [{"type": "photo", "url": remote}],
    })
    with patch("fetching.media.urlopen", side_effect=AssertionError("must not fetch")):
        assert archive_batch(25) == 0


@pytest.mark.django_db
def test_archive_batch_stores_a_new_photo(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    remote = "https://pbs.twimg.com/media/a.jpg"
    TwitterUser.objects.create(handle="jack", tracking=True)
    upsert_tweet({
        "rest_id": "1", "author_id": "1", "account": "jack",
        "text": "pic", "created_at": "Wed Oct 10 20:19:24 +0000 2018",
        "media": [{"type": "photo", "url": remote}],
    })

    class _Resp(BytesIO):
        headers = {"Content-Length": "4"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with patch("fetching.media.urlopen", return_value=_Resp(b"jpeg")):
        assert archive_batch(25) == 1
    asset = MediaAsset.objects.get(remote_url=remote)
    assert (Path(tmp_path) / asset.relative_path).read_bytes() == b"jpeg"


@pytest.mark.django_db
def test_lookup_skips_rows_whose_file_is_gone(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    MediaAsset.objects.create(
        remote_url="https://pbs.twimg.com/media/a.jpg",
        relative_path="ab/missing.jpg",
    )
    assert lookup_local_urls(["https://pbs.twimg.com/media/a.jpg"]) == {}


@pytest.mark.django_db
def test_archive_batch_stores_account_avatars(settings, tmp_path):
    """Avatars are the images on every card, and were the last ones left on X."""
    settings.MEDIA_ROOT = tmp_path
    remote = "https://pbs.twimg.com/profile_images/1/abc_normal.jpg"
    TwitterUser.objects.create(handle="jack", tracking=True, avatar_url=remote)

    class _Resp(BytesIO):
        headers = {"Content-Length": "4"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with patch("fetching.media.urlopen", return_value=_Resp(b"jpeg")):
        assert archive_batch(25) == 1

    asset = MediaAsset.objects.get(remote_url=remote)
    assert (Path(tmp_path) / asset.relative_path).read_bytes() == b"jpeg"


@pytest.mark.django_db
def test_the_feed_serves_an_archived_avatar_from_our_own_origin(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.MEDIA_URL = "/media/"
    remote = "https://pbs.twimg.com/profile_images/1/abc_normal.jpg"
    relative = relative_path_for(remote)
    dest = Path(tmp_path) / relative
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"jpeg")
    MediaAsset.objects.create(remote_url=remote, relative_path=relative)
    TwitterUser.objects.create(handle="jack", tracking=True, avatar_url=remote)
    upsert_tweet({
        "rest_id": "1", "author_id": "1", "account": "jack",
        "text": "hello", "created_at": "Wed Oct 10 20:19:24 +0000 2018",
    })
    user = User.objects.create_user(username="alice", password="pw", is_staff=True)
    client = APIClient()
    client.force_authenticate(user=user)

    row = client.get("/api/feed/").data["results"][0]

    assert row["author"]["avatar_url"] == f"/media/{relative}"


@pytest.mark.django_db
def test_an_unarchived_avatar_still_falls_back_to_x(settings, tmp_path):
    """A brand-new account shows a picture before the archiver has caught up."""
    settings.MEDIA_ROOT = tmp_path
    remote = "https://pbs.twimg.com/profile_images/2/def_normal.jpg"
    TwitterUser.objects.create(handle="jack", tracking=True, avatar_url=remote)
    upsert_tweet({
        "rest_id": "1", "author_id": "1", "account": "jack",
        "text": "hello", "created_at": "Wed Oct 10 20:19:24 +0000 2018",
    })
    user = User.objects.create_user(username="alice", password="pw", is_staff=True)
    client = APIClient()
    client.force_authenticate(user=user)

    row = client.get("/api/feed/").data["results"][0]

    assert row["author"]["avatar_url"] == remote
