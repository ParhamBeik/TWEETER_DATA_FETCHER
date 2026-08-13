"""Unit test: fetcher state round-trips through Postgres KeyValueState.

The subprocess runner seeds a scratch state dir from Postgres before a run and
writes it back after. This verifies persist -> restore reproduces the blob, the
core of the "Postgres is the sole durable store" guarantee.
"""
import json
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from apps.fetching import runner
from apps.tweets.models import EndpointState, FetchRun, KeyValueState, RawPage, XSession


@pytest.mark.django_db
def test_sync_state_persist_then_restore_round_trips():
    # Arrange: a run wrote a sync_state.json into its scratch dir.
    src = Path(tempfile.mkdtemp(prefix="tdf_src_"))
    state_dir = src / "data" / "historical_live" / "state"
    state_dir.mkdir(parents=True)
    watermark = {"watermark": "1700000000", "cursor": "abc"}
    (state_dir / "sync_state.json").write_text(json.dumps(watermark), encoding="utf-8")

    # Act 1: persist to Postgres.
    runner._persist_state(src, "historical")
    row = KeyValueState.objects.get(namespace="sync_state", name="historical_live")
    assert row.data == watermark

    # Act 2: restore into a fresh scratch dir.
    dst = Path(tempfile.mkdtemp(prefix="tdf_dst_"))
    runner._restore_state(dst, "live")  # live shares the historical_live sub
    restored = json.loads(
        (dst / "data" / "historical_live" / "state" / "sync_state.json").read_text()
    )
    assert restored == watermark


@pytest.mark.django_db
def test_request_state_files_round_trip():
    src = Path(tempfile.mkdtemp(prefix="tdf_src_"))
    state_dir = src / "data" / "search" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "tx_health.json").write_text(json.dumps({"ok": True}), encoding="utf-8")

    runner._persist_state(src, "search")
    assert KeyValueState.objects.filter(
        namespace="request_state", name="search:tx_health.json"
    ).exists()

    dst = Path(tempfile.mkdtemp(prefix="tdf_dst_"))
    runner._restore_state(dst, "search")
    restored = json.loads(
        (dst / "data" / "search" / "state" / "tx_health.json").read_text()
    )
    assert restored == {"ok": True}


@pytest.mark.django_db
def test_active_session_maps_to_fetcher_config_keys(monkeypatch):
    seed = Path(tempfile.mkdtemp(prefix="tdf_seed_"))
    (seed / "config.example.json").write_text(
        json.dumps({"api_auth": {"bearer_token": "old"}, "api_cookies": {"old": "cookie"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "SEED_DIR", seed)
    XSession.objects.create(
        cookies={"auth_token": "cookie", "ct0": "csrf"},
        headers={"authorization": "Bearer fresh-token", "x-csrf-token": "csrf"},
    )

    config = json.loads(runner._write_config(Path(tempfile.mkdtemp(prefix="tdf_cfg_"))).read_text())

    assert config["api_cookies"]["auth_token"] == "cookie"
    assert config["api_headers"]["x-csrf-token"] == "csrf"
    assert config["api_auth"]["bearer_token"] == "fresh-token"
    assert "auth" not in config


@pytest.mark.django_db
def test_write_config_materializes_tracked_db_tiers(monkeypatch):
    from apps.tweets.models import TwitterUser

    seed = Path(tempfile.mkdtemp(prefix="tdf_seed_"))
    (seed / "config.example.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runner, "SEED_DIR", seed)
    TwitterUser.objects.create(handle="jack", tracking=True, priority=1, display_name="Jack")
    TwitterUser.objects.create(handle="elon", tracking=False, priority=2)

    root = Path(tempfile.mkdtemp(prefix="tdf_cfg_"))
    runner._write_config(root)
    accounts = json.loads((root / "config" / "accounts.json").read_text())
    assert accounts["priority_1"] == [{"username": "jack", "display_name": "Jack"}]
    assert accounts["priority_2"] == []


@pytest.mark.django_db
def test_run_artifacts_persist_raw_pages_state_ledger_and_retention():
    root = Path(tempfile.mkdtemp(prefix="tdf_artifacts_"))
    raw = root / "data" / "historical_live" / "raw" / "UserTweets" / "jack" / "batch"
    raw.mkdir(parents=True)
    (raw / "page_1.json").write_text(json.dumps({"data": {"ok": True}}), encoding="utf-8")
    state = root / "data" / "historical_live" / "state"
    state.mkdir(parents=True)
    (state / "sync_state.json").write_text(
        json.dumps({"jack": {"UserTweets": {"status": "completed"}}}), encoding="utf-8"
    )
    logs = root / "data" / "historical_live" / "logs"
    logs.mkdir(parents=True)
    (logs / "http_summary.json").write_text(
        json.dumps({"failure_ledger": {"UserTweets:404": {"count": 1}}}), encoding="utf-8"
    )
    reports = root / "data" / "historical_live" / "reports"
    reports.mkdir(parents=True)
    (reports / "run.json").write_text(
        json.dumps({"summary": {"successful_endpoints": 1, "partial_endpoints": 0, "failed_endpoints": 0}}),
        encoding="utf-8",
    )
    run = FetchRun.objects.create(run_id="test-run", subsystem="historical")
    old = RawPage.objects.create(endpoint="Old", account="old", batch="old", page_number=1)
    RawPage.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=8))

    summary, ledger, status = runner._persist_artifacts(root, "historical", run, 0)

    assert status == "completed"
    assert summary["raw_pages"] == 1
    assert ledger["UserTweets:404"]["count"] == 1
    assert RawPage.objects.filter(endpoint="UserTweets", fetch_run=run).exists()
    assert not RawPage.objects.filter(pk=old.pk).exists()
    assert EndpointState.objects.get(account="jack", endpoint="UserTweets").data["status"] == "completed"


@pytest.mark.django_db
def test_nonzero_subprocess_exit_persists_failed_run(monkeypatch):
    class FailedProcess:
        stdout = ["fetch failed\n"]

        @staticmethod
        def wait():
            return 7

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: FailedProcess())

    result = runner.run_fetcher("fake.module", [], "search", target="test:Latest")
    result.run.refresh_from_db()

    assert result.run.status == "failed"
    assert result.run.return_code == 7
    assert "fetch failed" in result.run.log_excerpt
    runner.cleanup(result.root)


@pytest.mark.django_db
def test_request_state_is_keyed_by_subsystem():
    live = Path(tempfile.mkdtemp(prefix="tdf_live_"))
    search = Path(tempfile.mkdtemp(prefix="tdf_search_"))
    (live / "data" / "historical_live" / "state").mkdir(parents=True)
    (search / "data" / "search" / "state").mkdir(parents=True)
    (live / "data" / "historical_live" / "state" / "live_state.json").write_text(
        json.dumps({"jack": {"ok": True}}), encoding="utf-8"
    )
    (search / "data" / "search" / "state" / "live_state.json").write_text(
        json.dumps({"should_not_leak": True}), encoding="utf-8"
    )

    runner._persist_state(live, "live")
    runner._persist_state(search, "search")

    dst = Path(tempfile.mkdtemp(prefix="tdf_dst_"))
    runner._restore_state(dst, "search")
    restored = (dst / "data" / "search" / "state" / "live_state.json").read_text()
    assert json.loads(restored) == {"should_not_leak": True}


@pytest.mark.django_db
def test_persist_session_reads_refreshed_scratch_config():
    root = Path(tempfile.mkdtemp(prefix="tdf_sess_"))
    (root / "config").mkdir(parents=True)
    (root / "config" / "config.json").write_text(
        json.dumps({
            "api_cookies": {"auth_token": "new", "ct0": "csrf"},
            "api_headers": {"authorization": "Bearer new"},
        }),
        encoding="utf-8",
    )
    session = XSession.objects.create(
        cookies={"auth_token": "old"},
        headers={"authorization": "Bearer old"},
    )
    runner._persist_session(root)
    session.refresh_from_db()
    assert session.cookies["auth_token"] == "new"
    assert session.headers["authorization"] == "Bearer new"


def test_iter_search_tweets_scopes_to_product():
    root = Path(tempfile.mkdtemp(prefix="tdf_search_prod_"))
    latest = root / "data" / "search" / "processed" / "ai" / "latest"
    top = root / "data" / "search" / "processed" / "ai" / "top"
    latest.mkdir(parents=True)
    top.mkdir(parents=True)
    (latest / "ai.json").write_text(
        json.dumps({"tweets": [{"id": "latest"}]}), encoding="utf-8"
    )
    (top / "ai.json").write_text(
        json.dumps({"tweets": [{"id": "top"}]}), encoding="utf-8"
    )
    ids = [item["id"] for item in runner.iter_search_tweets(root, "ai", "Latest")]
    assert ids == ["latest"]
