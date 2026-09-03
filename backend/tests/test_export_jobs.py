"""Feed export as a job rather than a streamed response.

The old endpoint streamed an unbounded queryset straight out of the view. With
`gunicorn --workers 2`, two concurrent full-archive exports occupied both and
the console stopped answering. The work now runs on the control worker; what
this file pins is that moving it did not lose the export, and that the two new
failure modes it introduces -- a bounded result and a file behind auth -- behave
as intended.

Celery is eager in the test settings, so `run_export.delay` runs inline.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from fetching.ingest import upsert_tweet
from tweets.models import ExportJob, TwitterUser


@pytest.fixture
def owner(db):
    user = User.objects.create_user(username="owner", password="pw")
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def _post(rest_id: str, account: str = "jack", text: str = "hello") -> None:
    upsert_tweet(
        {
            "rest_id": rest_id,
            "author_id": "1",
            "account": account,
            "text": text,
            "created_at": "Wed Oct 10 20:19:24 +0000 2018",
            "likes": 2,
        }
    )
    TwitterUser.objects.update_or_create(handle=account, defaults={"tracking": True})


@pytest.mark.django_db
def test_export_runs_as_a_job_and_downloads(owner, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    client, _user = owner
    _post("1")
    _post("2")

    queued = client.post("/api/export/", {"format": "jsonl", "query": ""}, format="json")
    assert queued.status_code == 202
    job_id = queued.json()["id"]

    detail = client.get(f"/api/export/{job_id}/").json()
    assert detail["status"] == "completed"
    assert detail["row_count"] == 2
    assert detail["truncated"] is False

    download = client.get(detail["download_url"])
    assert download.status_code == 200
    body = b"".join(download.streaming_content).decode()
    rows = [json.loads(line) for line in body.splitlines()]
    assert {row["tweet_id"] for row in rows} == {"1", "2"}
    # Content-Disposition carries a readable name, not the on-disk token.
    assert "tweets_" in download["Content-Disposition"]


@pytest.mark.django_db
def test_csv_export_carries_every_engagement_column(owner, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    client, _user = owner
    _post("1")

    job = client.post("/api/export/", {"format": "csv", "query": ""}, format="json").json()
    download = client.get(f"/api/export/{job['id']}/download/")
    header = b"".join(download.streaming_content).decode().splitlines()[0]
    # An export that silently drops replies and quotes makes offline analysis
    # disagree with the console for no visible reason.
    for column in ("likes", "retweets", "replies", "quotes", "bookmarks", "views"):
        assert column in header


@pytest.mark.django_db
def test_export_is_bounded_and_says_so(owner, settings, tmp_path):
    """The row ceiling is what makes 'All time, no filters' a finite job."""
    settings.MEDIA_ROOT = tmp_path
    settings.EXPORT_MAX_ROWS = 3
    client, _user = owner
    for index in range(6):
        _post(str(index))

    job = client.post("/api/export/", {"format": "jsonl", "query": ""}, format="json").json()
    detail = client.get(f"/api/export/{job['id']}/").json()
    assert detail["row_count"] == 3
    # Truncation is reported, not silent -- a prefix of the answer presented as
    # the answer is the failure mode worth guarding against.
    assert detail["truncated"] is True


@pytest.mark.django_db
def test_export_honours_the_feed_filters_it_was_asked_for(owner, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    client, _user = owner
    _post("1", account="jack")
    _post("2", account="elon")

    job = client.post(
        "/api/export/", {"format": "jsonl", "query": "account=elon"}, format="json"
    ).json()
    download = client.get(f"/api/export/{job['id']}/download/")
    rows = [json.loads(line) for line in b"".join(download.streaming_content).decode().splitlines()]
    assert [row["account"] for row in rows] == ["elon"]


@pytest.mark.django_db
def test_repeatable_account_filter_survives_the_round_trip(owner, settings, tmp_path):
    """Stored as a query string, not a dict: a dict flattens ?a=1&a=2 to one
    value, which is exactly the bug the feed itself had.
    """
    settings.MEDIA_ROOT = tmp_path
    client, _user = owner
    for handle in ("jack", "elon", "someone"):
        _post({"jack": "1", "elon": "2", "someone": "3"}[handle], account=handle)

    job = client.post(
        "/api/export/",
        {"format": "jsonl", "query": "account=jack&account=elon"},
        format="json",
    ).json()
    download = client.get(f"/api/export/{job['id']}/download/")
    rows = [json.loads(line) for line in b"".join(download.streaming_content).decode().splitlines()]
    assert {row["account"] for row in rows} == {"jack", "elon"}


@pytest.mark.django_db
def test_another_user_cannot_read_someone_elses_export(owner, settings, tmp_path):
    """The unguessable filename is not the access control; this is."""
    settings.MEDIA_ROOT = tmp_path
    client, _user = owner
    _post("1")
    job = client.post("/api/export/", {"format": "jsonl", "query": ""}, format="json").json()

    intruder = APIClient()
    intruder.force_authenticate(user=User.objects.create_user(username="other", password="pw"))
    assert intruder.get(f"/api/export/{job['id']}/").status_code == 404
    assert intruder.get(f"/api/export/{job['id']}/download/").status_code == 404


@pytest.mark.django_db
def test_expired_file_reports_gone_rather_than_crashing(owner, settings, tmp_path):
    """The TTL purge can reach a file while its row is still being polled."""
    settings.MEDIA_ROOT = tmp_path
    client, _user = owner
    _post("1")
    job = client.post("/api/export/", {"format": "jsonl", "query": ""}, format="json").json()

    row = ExportJob.objects.get(pk=job["id"])
    (tmp_path / row.relative_path).unlink()

    response = client.get(f"/api/export/{job['id']}/download/")
    assert response.status_code == 410
    assert "expired" in response.json()["detail"]


@pytest.mark.django_db
def test_purge_removes_both_the_row_and_the_file(owner, settings, tmp_path):
    """Either half left behind is a bug: a row without a file is a broken link,
    a file without a row is bytes nothing will ever reclaim.
    """
    from datetime import timedelta

    from django.utils import timezone

    from fetching.tasks import purge_old_exports

    settings.MEDIA_ROOT = tmp_path
    settings.EXPORT_TTL_HOURS = 24
    client, _user = owner
    _post("1")
    job = client.post("/api/export/", {"format": "jsonl", "query": ""}, format="json").json()
    row = ExportJob.objects.get(pk=job["id"])
    path = tmp_path / row.relative_path
    assert path.is_file()

    ExportJob.objects.filter(pk=row.pk).update(
        created_at=timezone.now() - timedelta(hours=25)
    )
    assert purge_old_exports() == 1
    assert not ExportJob.objects.filter(pk=row.pk).exists()
    assert not path.exists()


@pytest.mark.django_db
def test_export_requires_a_signed_in_user(db):
    assert APIClient().post("/api/export/", {"format": "jsonl"}, format="json").status_code == 401
