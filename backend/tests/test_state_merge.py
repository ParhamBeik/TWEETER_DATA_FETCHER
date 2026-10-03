"""Live and historical share the `historical_live` state blobs and run at once.

Each run restores the blobs at start and persists them at the end. Persisting
them whole meant the run that finished last rolled back the other's work: the
archive walk's backfill cursor, or an operator's cleared quarantine. Persist
now applies only what this run changed.
"""
import json

import pytest

from fetching import runner
from fetching.accounts import clear_live_quarantine, live_state_map
from tweets.models import EndpointState, KeyValueState


def _state_dir(root):
    return root / "data" / "historical_live" / "state"


def _edit(root, filename, change):
    path = _state_dir(root) / filename
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


@pytest.mark.django_db
def test_concurrent_live_and_historical_keep_each_others_progress(tmp_path):
    KeyValueState.objects.create(namespace="sync_state", name="historical_live", data={
        "alice": {"UserTweets": {"fetch_watermark": "2026-10-01T00:00:00Z", "backfill_cursor": "B0"}},
    })
    KeyValueState.objects.create(
        namespace="request_state", name="historical_live:live_state.json",
        data={"alice": {"quarantined": True, "quarantine_reason": "x"}},
    )
    hist, live = tmp_path / "hist", tmp_path / "live"
    runner._restore_state(hist, "historical")
    runner._restore_state(live, "live")

    def walk(data):
        data["alice"]["UserTweets"].update(backfill_cursor="B1", backfill_pages_done=25)

    _edit(hist, "sync_state.json", walk)
    runner._persist_state(hist, "historical")

    # The operator clears quarantine while the live run is still going.
    clear_live_quarantine("alice")

    def poll(data):
        data["alice"]["UserTweets"]["fetch_watermark"] = "2026-10-01T05:00:00Z"

    _edit(live, "sync_state.json", poll)
    runner._persist_state(live, "live")
    runner._persist_endpoint_states(live, "live")

    blob = KeyValueState.objects.get(namespace="sync_state").data["alice"]["UserTweets"]
    assert blob == {
        "fetch_watermark": "2026-10-01T05:00:00Z",
        "backfill_cursor": "B1",
        "backfill_pages_done": 25,
    }
    # The EndpointState mirror is written from the merged state, not the stale copy.
    assert EndpointState.objects.get(account="alice").data == blob
    assert live_state_map()["alice"]["quarantined"] is False


def test_merge_applies_only_this_runs_changes():
    base = {"a": 1, "b": {"x": 1, "y": 1}, "gone": 1, "kept": 1}
    ours = {"a": 2, "b": {"x": 2, "y": 1}, "kept": 1, "new": 1}  # dropped "gone"
    theirs = {"a": 1, "b": {"x": 1, "y": 9}, "gone": 1, "kept": 5, "theirs": 1}

    assert runner._merge_state(base, ours, theirs, depth=1) == {
        "a": 2,                    # ours changed it
        "b": {"x": 2, "y": 9},     # nested: each side's change survives
        "kept": 5,                 # only theirs changed it
        "new": 1,
        "theirs": 1,
    }


def test_merge_does_not_drop_a_key_the_other_side_changed():
    assert runner._merge_state({"k": 1}, {}, {"k": 2}) == {"k": 2}
    assert runner._merge_state({"k": 1}, {}, {"k": 1}) == {}


def test_merge_gives_a_key_both_sides_changed_to_this_run():
    assert runner._merge_state({"k": 1}, {"k": 2}, {"k": 3}) == {"k": 2}


@pytest.mark.django_db
def test_first_persist_without_a_row_writes_the_runs_state(tmp_path):
    runner._restore_state(tmp_path, "live")
    (_state_dir(tmp_path) / "live_state.json").write_text(json.dumps({"bob": {"x": 1}}))
    runner._persist_state(tmp_path, "live")
    assert KeyValueState.objects.get(name="historical_live:live_state.json").data == {"bob": {"x": 1}}


def test_merge_replaces_a_request_state_record_whole():
    """A rate limit's remaining belongs to its reset; never splice them."""
    base = {"UserTweets": {"remaining": 50, "reset": 1, "limit": 150}}
    ours = {"UserTweets": {"remaining": 0, "reset": 1, "limit": 150}}       # live, window 1
    theirs = {"UserTweets": {"remaining": 140, "reset": 2, "limit": 150}}   # historical, window 2
    assert runner._merge_state(base, ours, theirs) == ours


@pytest.mark.django_db
def test_persist_merges_sync_state_to_fields_but_rate_limits_by_record(tmp_path):
    KeyValueState.objects.create(
        namespace="request_state", name="historical_live:rate_limits.json",
        data={"UserTweets": {"remaining": 50, "reset": 1}},
    )
    runner._restore_state(tmp_path, "live")
    KeyValueState.objects.filter(name="historical_live:rate_limits.json").update(
        data={"UserTweets": {"remaining": 140, "reset": 2}}
    )
    _edit(tmp_path, "rate_limits.json", lambda d: d["UserTweets"].update(remaining=0))
    runner._persist_state(tmp_path, "live")
    assert KeyValueState.objects.get(name="historical_live:rate_limits.json").data == {
        "UserTweets": {"remaining": 0, "reset": 1}
    }


@pytest.mark.django_db
def test_persist_merges_into_a_row_another_run_created_meanwhile(tmp_path):
    """No row at lookup, one by insert: merge into it, don't fail on the key."""
    from unittest.mock import MagicMock, patch

    runner._restore_state(tmp_path, "live")
    (_state_dir(tmp_path) / "endpoint_health.json").write_text(json.dumps({"UserTweets": "healthy"}))
    # The other run's insert, committed between our locked lookup and our create.
    KeyValueState.objects.create(
        namespace="request_state", name="historical_live:endpoint_health.json",
        data={"Other": "suspect"},
    )
    real = KeyValueState.objects.select_for_update
    missed = MagicMock()
    missed.filter.return_value.first.return_value = None
    lookups = iter([missed])

    def select_for_update():
        return next(lookups, None) or real()

    with patch.object(KeyValueState.objects, "select_for_update", side_effect=select_for_update):
        runner._persist_state(tmp_path, "live")
    assert KeyValueState.objects.get(name="historical_live:endpoint_health.json").data == {
        "Other": "suspect", "UserTweets": "healthy",
    }
