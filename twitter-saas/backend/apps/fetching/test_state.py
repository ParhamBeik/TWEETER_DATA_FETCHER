"""Unit test: fetcher state round-trips through Postgres KeyValueState.

The subprocess runner seeds a scratch state dir from Postgres before a run and
writes it back after. This verifies persist -> restore reproduces the blob, the
core of the "Postgres is the sole durable store" guarantee.
"""
import json
import tempfile
from pathlib import Path

import pytest

from apps.fetching import runner
from apps.tweets.models import KeyValueState


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
        namespace="request_state", name="tx_health.json"
    ).exists()

    dst = Path(tempfile.mkdtemp(prefix="tdf_dst_"))
    runner._restore_state(dst, "search")
    restored = json.loads(
        (dst / "data" / "search" / "state" / "tx_health.json").read_text()
    )
    assert restored == {"ok": True}
