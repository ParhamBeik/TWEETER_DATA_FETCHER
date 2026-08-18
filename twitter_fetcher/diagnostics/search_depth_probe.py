#!/usr/bin/env python3
"""Measure SearchTimeline yield across product / window / depth combinations.

SearchTimeline page 2+ is gated server-side over HTTP (see
reports/SEARCHTIMELINE_404_ROOT_CAUSE.md), so depth comes from the Playwright
hybrid. This probe answers which knobs actually raise tweet yield, so the
production search config is set from evidence rather than guesswork.

Run inside the SaaS image (it needs a live XSession-materialized config):
    python search_depth_probe.py --raw-query "<query>" [--slug probe]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tweeter_data_fetcher.pipelines.search.service import SearchTimelineMonitor


def probe(monitor, slug, raw_query, product, rolling_hours, depth):
    search_def = {
        "name": slug,
        "slug": slug,
        "enabled": True,
        "product": product,
        "preserve_exact_query": True,
        "raw_query": raw_query,
        "pagination_depth": depth,
        "max_retries": 2,
        "rolling_hours": rolling_hours,
    }
    started = time.time()
    try:
        result = monitor.monitor_search(search_def)
    except Exception as exc:  # noqa: BLE001 - probe reports, never raises
        return {
            "product": product, "rolling_hours": rolling_hours, "depth": depth,
            "error": f"{type(exc).__name__}: {str(exc)[:120]}",
        }
    counts = result.get("counts") or {}
    return {
        "product": product,
        "rolling_hours": rolling_hours,
        "depth": depth,
        "tweets": counts.get("tweets", 0),
        "pages": result.get("pages_fetched") or len(result.get("pages", []) or []),
        "status": result.get("status"),
        "transport": result.get("transport"),
        "exhausted_reason": (result.get("metadata") or {}).get("exhausted_reason"),
        "seconds": round(time.time() - started, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-query", required=True)
    parser.add_argument("--slug", default="probe")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    monitor = SearchTimelineMonitor(config_path=args.config)
    rows = []
    # One knob at a time against the same query.
    for product, hours, depth in [
        ("Top", 24, 3),       # current production setting
        ("Top", 720, 10),     # wider window, deeper cap
        ("Latest", 24, 3),    # chronological, current window
        ("Latest", 720, 10),  # chronological, wide window
    ]:
        row = probe(monitor, args.slug, args.raw_query, product, hours, depth)
        rows.append(row)
        print(json.dumps(row), flush=True)
    print("\nSUMMARY")
    for row in rows:
        print(
            f"  {row.get('product'):7} hours={row.get('rolling_hours'):<4} depth={row.get('depth'):<3}"
            f" -> tweets={row.get('tweets', '-'):<5} pages={row.get('pages', '-'):<4}"
            f" {row.get('exhausted_reason') or row.get('error') or ''}"
        )


if __name__ == "__main__":
    main()
