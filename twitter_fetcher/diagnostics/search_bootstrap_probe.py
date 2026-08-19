#!/usr/bin/env python3
"""Run the production browser-bootstrap path for a search and report why it stopped.

The pipeline reports only `partial_browser_stalled`; this exposes the underlying
BrowserBootstrap fields (stop_reason, captured page count, route retries) plus
the search_url actually used, so a shallow deep-search run can be attributed to
the right cause instead of guessed at.

Run inside the SaaS worker image:
    python search_bootstrap_probe.py <config.json> [--max-pages 8]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, "/app/fetcher")

from tweeter_data_fetcher.pipelines.search.service import (  # noqa: E402
    SearchQueryBuilder,
    SearchTimelineMonitor,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--no-predicate", action="store_true")
    args = parser.parse_args()

    monitor = SearchTimelineMonitor(config_path=args.config)
    searches_file = pathlib.Path(args.config).with_name("searches.json")
    definitions = json.loads(searches_file.read_text(encoding="utf-8"))
    search_def = definitions[0] if definitions else None
    if search_def is None:
        raise SystemExit("no search definitions in config")

    raw_query = SearchQueryBuilder.build_raw_query(search_def, datetime.utcnow())
    search_url = SearchQueryBuilder.build_human_search_url(
        raw_query, str(search_def.get("product") or "Top")
    )
    print(f"search_url = {search_url}")
    print(f"product    = {search_def.get('product')}")
    print(f"depth      = {search_def.get('pagination_depth')}  rolling_hours={search_def.get('rolling_hours')}")

    result = monitor.fetcher.bootstrap_browser_context(
        search_url=search_url,
        capture_endpoint="SearchTimeline",
        max_pages=args.max_pages,
        stop_when=None,
    )
    pages = result.target_pages.get("SearchTimeline", [])
    print(f"ok={result.ok} stop_reason={result.stop_reason!r} route_retries={result.route_retry_count}")
    print(f"captured_pages={len(pages)} support_requests={result.support_request_count}")
    print(f"error={result.error}")
    for i, meta in enumerate(result.target_page_meta.get("SearchTimeline", []), 1):
        print(f"  page{i}: status={meta.get('status')} has_cursor={meta.get('has_input_cursor')}")


if __name__ == "__main__":
    main()
