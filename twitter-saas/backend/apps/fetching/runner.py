"""Run the vendored fetcher in an isolated subprocess and ingest results.

Each run gets an ephemeral scratch PROJECT_ROOT (TDF_PROJECT_ROOT) so the
engine's on-disk layer never persists. Durable continuity (sync-state
watermarks/cursors, tx/query health) is round-tripped through Postgres
KeyValueState before and after the run. Postgres is the sole durable store.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from django.conf import settings

from apps.tweets.models import KeyValueState, XSession

logger = logging.getLogger(__name__)

VENDOR_DIR = Path(settings.BASE_DIR) / "apps" / "fetching" / "vendor"
SEED_DIR = Path(settings.BASE_DIR) / "seed"

# Mirrors SearchQueryBuilder.slug in the vendored engine so the runner can
# predict the on-disk output path the engine writes processed search exports to.
_SLUG_KEEP = re.compile(r"[^A-Za-z0-9_\\-]+")


def normalize_slug(raw: str) -> str:
    """Normalize a search slug the same way the vendored SearchQueryBuilder does.

    The engine writes processed exports under ``data/search/processed/<slug>/``
    where ``<slug>`` is the normalized form (lowercased, non-alnum runs → ``_``).
    The runner must use the same form to locate those files.
    """
    return _SLUG_KEEP.sub("_", str(raw)).strip("_").lower() or "search_timeline"


def _active_session() -> XSession | None:
    return XSession.objects.filter(active=True).first()


def _search_def(search) -> dict:
    """Minimal searches.json entry the vendored search pipeline understands."""
    return {
        "name": search.name,
        "slug": search.slug,
        "enabled": True,
        "product": search.product,
        "preserve_exact_query": True,
        "raw_query": search.raw_query,
        "pagination_depth": 3,
        "max_retries": 3,
        "rolling_hours": 24,
    }


def _write_config(root: Path, searches: list | None = None) -> Path:
    """Materialize config/config.json from the shared XSession + seed files."""
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    src = SEED_DIR / "accounts.json"
    if src.exists():
        shutil.copy2(src, config_dir / "accounts.json")
    # searches.json comes from the DB when provided, else the seed file.
    if searches is not None:
        (config_dir / "searches.json").write_text(
            json.dumps([_search_def(s) for s in searches], indent=2), encoding="utf-8"
        )
    elif (SEED_DIR / "searches.json").exists():
        shutil.copy2(SEED_DIR / "searches.json", config_dir / "searches.json")

    base = {}
    example = SEED_DIR / "config.example.json"
    if example.exists():
        base = json.loads(example.read_text(encoding="utf-8"))
    session = _active_session()
    if session:
        base.setdefault("auth", {})
        base["auth"]["cookies"] = session.cookies
        base["auth"]["headers"] = session.headers
    (config_dir / "config.json").write_text(json.dumps(base, indent=2), encoding="utf-8")
    return config_dir / "config.json"


def _restore_state(root: Path, subsystem: str) -> None:
    """Seed the scratch state dir from Postgres so watermarks/cursors persist."""
    sub = "historical_live" if subsystem in ("historical", "live") else subsystem
    state_dir = root / "data" / sub / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sync = KeyValueState.objects.filter(namespace="sync_state", name=sub).first()
    if sync:
        (state_dir / "sync_state.json").write_text(json.dumps(sync.data), encoding="utf-8")
    for row in KeyValueState.objects.filter(namespace="request_state"):
        (state_dir / row.name).write_text(json.dumps(row.data), encoding="utf-8")


def _persist_state(root: Path, subsystem: str) -> None:
    sub = "historical_live" if subsystem in ("historical", "live") else subsystem
    state_dir = root / "data" / sub / "state"
    sync_file = state_dir / "sync_state.json"
    if sync_file.exists():
        KeyValueState.objects.update_or_create(
            namespace="sync_state", name=sub,
            defaults={"data": json.loads(sync_file.read_text(encoding="utf-8"))},
        )
    for f in state_dir.glob("*.json"):
        if f.name == "sync_state.json":
            continue
        KeyValueState.objects.update_or_create(
            namespace="request_state", name=f.name,
            defaults={"data": json.loads(f.read_text(encoding="utf-8"))},
        )


def run_fetcher(module: str, args: list[str], subsystem: str, searches: list | None = None) -> Path:
    """Run a vendored pipeline module as a subprocess; return the scratch root.

    Caller ingests from the returned root, then calls cleanup(root). On any
    internal failure (config write, state restore, persist) the scratch dir is
    cleaned up here so it never leaks. A non-zero subprocess exit is logged but
    does NOT raise — the engine may have written partial results before failing,
    and those are still worth ingesting.
    """
    root = Path(tempfile.mkdtemp(prefix="tdf_run_"))
    try:
        config_path = _write_config(root, searches=searches)
        _restore_state(root, subsystem)

        env = dict(os.environ)
        env["TDF_PROJECT_ROOT"] = str(root)
        env["TDF_CONFIG"] = str(config_path)
        env["PYTHONPATH"] = str(VENDOR_DIR) + os.pathsep + env.get("PYTHONPATH", "")

        result = subprocess.run(
            [sys.executable, "-m", module, *args],
            env=env,
            cwd=str(root),
            check=False,
        )
        _persist_state(root, subsystem)
        if result.returncode != 0:
            logger.warning(
                "fetcher %s exited with code %s for subsystem=%s; "
                "ingesting whatever partial results exist",
                module,
                result.returncode,
                subsystem,
            )
        return root
    except Exception:
        # Never leak the scratch dir if something inside the try block raised
        # before the caller could take ownership of ``root``.
        shutil.rmtree(root, ignore_errors=True)
        raise


def cleanup(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)


def iter_processed_tweets(root: Path, subsystem: str) -> Iterable[dict]:
    """Yield normalized tweet dicts from the complete per-account set of a run.

    The historical/live engine writes seven processed sets per account
    (1_user_tweets .. 7_symmetric_difference); ``4_union`` is the complete
    superset, so reading only it avoids re-ingesting the six subset dirs.
    """
    sub = "historical_live" if subsystem in ("historical", "live") else subsystem
    union = root / "data" / sub / "processed" / "4_union"
    if not union.exists():
        return
    for json_file in union.rglob("*.json"):
        try:
            payload = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    yield item


def iter_search_tweets(root: Path, slug: str) -> Iterable[dict]:
    """Yield tweets from a search run's export ({slug}.json with a tweets key).

    ``slug`` is normalized the same way the vendored SearchQueryBuilder does it,
    because the engine writes processed exports under the normalized slug, not
    the raw model slug.
    """
    norm = normalize_slug(slug)
    processed = root / "data" / "search" / "processed" / norm
    if not processed.exists():
        return
    for json_file in processed.rglob(f"{norm}.json"):
        try:
            payload = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tweets = payload.get("tweets") if isinstance(payload, dict) else None
        if isinstance(tweets, list):
            for item in tweets:
                if isinstance(item, dict):
                    yield item
