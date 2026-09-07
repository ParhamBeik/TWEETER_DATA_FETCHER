"""Reopening a false depth-stop has to void both durable copies of walk state.

Integration at the component boundary: the command talks to two Postgres rows
(`EndpointState` and the engine `sync_state` blob). Persist copies the engine
blob onto every EndpointState row, which is how a one-sided reopen vanished on
the next tick. No HTTP, no subprocess.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError

from fetching.management.commands.reopen_false_depth_stops import (
    is_false_depth_stop,
    is_honest_completion,
    void_false_depth_stop,
)
from fetching.runner import _persist_endpoint_states, _persist_state, _restore_state
from fetching.tasks import _backfill_queue
from tweets.models import EndpointState, FetchRun, KeyValueState, TwitterUser

PARKED = {
    "backfill_complete": True,
    "backfill_depth_reason": None,
    "backfill_last_outcome": "success_timeline_exhausted",
    "backfill_completed_at": "2026-08-27T00:00:00Z",
    "backfill_cursor": None,
    "backfill_pages_done": 45,
    "backfill_stalled_ticks": 0,
    "backfill_empty_streak": 4,
}
HONEST = {
    "backfill_complete": True,
    "backfill_depth_reason": "reached_date_floor",
    "backfill_last_outcome": "success_window_complete",
    "backfill_completed_at": "2026-08-27T00:00:00Z",
    "backfill_cursor": None,
    "backfill_pages_done": 12,
    "backfill_stalled_ticks": 0,
    "backfill_empty_streak": 0,
}
HONEST_WALL = {
    "backfill_complete": True,
    "backfill_depth_reason": "provider_depth_limit",
    "backfill_last_outcome": "success_timeline_exhausted",
    "backfill_completed_at": "2026-08-27T00:00:00Z",
    "backfill_cursor": "c9",
    "backfill_pages_done": 45,
    "backfill_stalled_ticks": 0,
    "backfill_empty_streak": 0,
}


@pytest.fixture(autouse=True)
def _clear_cycle_locks():
    cache.clear()
    yield
    cache.clear()


def _user(handle: str, **kwargs) -> TwitterUser:
    return TwitterUser.objects.create(handle=handle, tracking=True, **kwargs)


def _endpoint(handle: str, data: dict) -> EndpointState:
    return EndpointState.objects.create(account=handle, endpoint="UserTweets", data=dict(data))


def _sync(accounts: dict[str, dict]) -> KeyValueState:
    blob = {
        handle: {"user_id": "1", "UserTweets": dict(data)}
        for handle, data in accounts.items()
    }
    return KeyValueState.objects.create(namespace="sync_state", name="historical_live", data=blob)


@pytest.mark.django_db
def test_command_voids_endpoint_state_and_engine_blob():
    _user("chigrl")
    _user("aaanews")
    _endpoint("chigrl", PARKED)
    _endpoint("aaanews", HONEST)
    _sync({"chigrl": PARKED, "aaanews": HONEST})

    call_command("reopen_false_depth_stops")

    parked = EndpointState.objects.get(account="chigrl", endpoint="UserTweets").data
    honest = EndpointState.objects.get(account="aaanews", endpoint="UserTweets").data
    blob = KeyValueState.objects.get(namespace="sync_state", name="historical_live").data
    assert parked["backfill_complete"] is False
    assert parked["backfill_last_outcome"] is None
    assert parked["backfill_pages_done"] == 0
    assert parked["backfill_empty_streak"] == 0
    assert honest["backfill_complete"] is True
    assert honest["backfill_depth_reason"] == "reached_date_floor"
    assert blob["chigrl"]["UserTweets"]["backfill_complete"] is False
    assert blob["aaanews"]["UserTweets"]["backfill_complete"] is True
    assert _backfill_queue(10) == ["chigrl"]


@pytest.mark.django_db
def test_command_recovers_when_only_the_engine_blob_is_still_parked():
    """The first production reopen cleared EndpointState, then persist restored it."""
    _user("chigrl")
    _endpoint("chigrl", void_false_depth_stop(PARKED))
    _sync({"chigrl": PARKED})

    call_command("reopen_false_depth_stops")

    blob = KeyValueState.objects.get(namespace="sync_state", name="historical_live").data
    assert blob["chigrl"]["UserTweets"]["backfill_complete"] is False
    assert _backfill_queue(10) == ["chigrl"]


@pytest.mark.django_db
def test_command_does_not_void_an_honest_engine_blob():
    _user("chigrl")
    _endpoint("chigrl", PARKED)
    _sync({"chigrl": HONEST})

    call_command("reopen_false_depth_stops")

    blob = KeyValueState.objects.get(namespace="sync_state", name="historical_live").data
    assert blob["chigrl"]["UserTweets"]["backfill_complete"] is True
    assert blob["chigrl"]["UserTweets"]["backfill_depth_reason"] == "reached_date_floor"
    aligned = EndpointState.objects.get(account="chigrl").data
    assert aligned["backfill_complete"] is True
    assert aligned["backfill_depth_reason"] == "reached_date_floor"
    assert aligned["backfill_last_outcome"] == "success_window_complete"
    assert _backfill_queue(10) == []


@pytest.mark.django_db
def test_dry_run_does_not_write():
    _user("chigrl")
    _endpoint("chigrl", PARKED)
    _sync({"chigrl": PARKED})

    call_command("reopen_false_depth_stops", dry_run=True)

    assert EndpointState.objects.get(account="chigrl").data["backfill_complete"] is True
    blob = KeyValueState.objects.get(namespace="sync_state", name="historical_live").data
    assert blob["chigrl"]["UserTweets"]["backfill_complete"] is True
    assert _backfill_queue(10) == []


@pytest.mark.django_db
def test_refuses_to_run_while_a_fetch_cycle_lock_is_held():
    _user("chigrl")
    _endpoint("chigrl", PARKED)
    _sync({"chigrl": PARKED})
    cache.add("fetch_cycle_lock:backfill_historical_all", "1", timeout=60)

    with pytest.raises(CommandError, match="fetch is running"):
        call_command("reopen_false_depth_stops")

    assert EndpointState.objects.get(account="chigrl").data["backfill_complete"] is True


@pytest.mark.django_db
def test_refuses_to_run_while_a_per_account_fetch_lock_is_held():
    _user("chigrl")
    _endpoint("chigrl", PARKED)
    _sync({"chigrl": PARKED})
    cache.add("fetch_cycle_lock:fetch_account_historical:chigrl", "1", timeout=60)

    with pytest.raises(CommandError, match="fetch is running"):
        call_command("reopen_false_depth_stops")

    assert EndpointState.objects.get(account="chigrl").data["backfill_complete"] is True


@pytest.mark.django_db
def test_refuses_to_run_while_a_fetch_run_is_in_progress():
    _user("chigrl")
    _endpoint("chigrl", PARKED)
    _sync({"chigrl": PARKED})
    FetchRun.objects.create(run_id="running-1", subsystem="historical", status="running")

    with pytest.raises(CommandError, match="fetch is running"):
        call_command("reopen_false_depth_stops")

    assert EndpointState.objects.get(account="chigrl").data["backfill_complete"] is True


@pytest.mark.django_db
def test_command_aligns_parked_blob_to_honest_endpoint():
    _user("chigrl")
    _endpoint("chigrl", HONEST)
    _sync({"chigrl": PARKED})

    call_command("reopen_false_depth_stops")

    blob = KeyValueState.objects.get(namespace="sync_state", name="historical_live").data
    assert blob["chigrl"]["UserTweets"]["backfill_complete"] is True
    assert blob["chigrl"]["UserTweets"]["backfill_depth_reason"] == "reached_date_floor"
    assert EndpointState.objects.get(account="chigrl").data["backfill_depth_reason"] == "reached_date_floor"
    assert _backfill_queue(10) == []


@pytest.mark.django_db
def test_post_fix_wall_with_cursor_is_left_alone():
    _user("chigrl")
    _endpoint("chigrl", HONEST_WALL)
    _sync({"chigrl": HONEST_WALL})

    call_command("reopen_false_depth_stops")

    data = EndpointState.objects.get(account="chigrl").data
    blob = KeyValueState.objects.get(namespace="sync_state", name="historical_live").data
    assert data["backfill_complete"] is True
    assert data["backfill_cursor"] == "c9"
    assert blob["chigrl"]["UserTweets"]["backfill_cursor"] == "c9"
    assert _backfill_queue(10) == []


@pytest.mark.django_db
def test_second_apply_does_not_rewrite_an_already_clean_blob():
    _user("chigrl")
    _endpoint("chigrl", PARKED)
    _sync({"chigrl": PARKED})
    call_command("reopen_false_depth_stops")
    kv = KeyValueState.objects.get(namespace="sync_state", name="historical_live")
    first_updated = kv.updated_at

    call_command("reopen_false_depth_stops")

    kv.refresh_from_db()
    assert kv.updated_at == first_updated
    assert kv.data["chigrl"]["UserTweets"]["backfill_complete"] is False


@pytest.mark.django_db
def test_dual_write_survives_restore_then_persist(tmp_path: Path):
    """After both copies are voided, the next tick must not repark the walk."""
    _user("chigrl")
    _endpoint("chigrl", PARKED)
    _sync({"chigrl": PARKED})
    call_command("reopen_false_depth_stops")
    root = tmp_path / "run"
    root.mkdir()

    _restore_state(root, "historical")
    _persist_state(root, "historical")
    _persist_endpoint_states(root, "historical")

    restored = json.loads(
        (root / "data" / "historical_live" / "state" / "sync_state.json").read_text()
    )
    assert restored["chigrl"]["UserTweets"]["backfill_complete"] is False
    data = EndpointState.objects.get(account="chigrl", endpoint="UserTweets").data
    assert data["backfill_complete"] is False
    blob = KeyValueState.objects.get(namespace="sync_state", name="historical_live").data
    assert blob["chigrl"]["UserTweets"]["backfill_complete"] is False
    assert _backfill_queue(10) == ["chigrl"]


def test_false_stop_helper_ignores_honest_completions():
    assert is_false_depth_stop(PARKED) is True
    assert is_false_depth_stop(HONEST) is False
    assert is_false_depth_stop(HONEST_WALL) is False
    assert is_honest_completion(HONEST_WALL) is True
    assert is_false_depth_stop(void_false_depth_stop(PARKED)) is False
    assert void_false_depth_stop(PARKED)["backfill_empty_streak"] == 0
