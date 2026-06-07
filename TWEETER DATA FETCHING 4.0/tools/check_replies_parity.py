#!/usr/bin/env python3
"""Offline parity check for v4 UserTweetsAndReplies requests vs test_replies_endpoint.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
DIAGNOSTIC = ROOT.parent / "test_replies_endpoint.py"
sys.path.insert(0, str(ROOT))


def load_diagnostic():
    spec = importlib.util.spec_from_file_location("test_replies_endpoint", DIAGNOSTIC)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {DIAGNOSTIC}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lower_headers(headers: Dict[str, Any]) -> Dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def query_payload(url: str) -> Dict[str, Any]:
    parsed = parse_qs(urlparse(url).query)
    return {key: json.loads(values[0]) for key, values in parsed.items() if values}


def main() -> None:
    from core.fetcher_engine import FetcherEngine

    diag = load_diagnostic()
    engine = FetcherEngine(config_path="config/config.json")
    query_id = engine.api_manager.get_query_id("UserTweetsAndReplies")
    variables = engine._timeline_variables("UserTweetsAndReplies", diag.USER_ID, None)
    features = engine._timeline_features("UserTweetsAndReplies")
    field_toggles = engine._timeline_field_toggles("UserTweetsAndReplies")
    v4_url = engine._build_graphql_url(
        endpoint="UserTweetsAndReplies",
        query_id=query_id,
        variables=variables,
        features=features,
        field_toggles=field_toggles,
    )
    v4_headers = lower_headers(
        engine.api_manager._build_request_headers(
            "UserTweetsAndReplies",
            username=diag.USERNAME,
            extra_headers={"referer": f"https://x.com/{diag.USERNAME}/with_replies", "x-twitter-active-user": "yes"},
        )
    )
    diag_url = diag.build_url(None)
    diag_headers = lower_headers(diag.build_headers())

    header_keys = [
        "accept",
        "accept-encoding",
        "accept-language",
        "authorization",
        "content-type",
        "cookie",
        "dnt",
        "priority",
        "referer",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "user-agent",
        "x-client-transaction-id",
        "x-csrf-token",
        "x-twitter-active-user",
        "x-twitter-auth-type",
        "x-twitter-client-language",
    ]

    mismatches = []
    if urlparse(v4_url).path != urlparse(diag_url).path:
        mismatches.append(("url.path", urlparse(v4_url).path, urlparse(diag_url).path))
    if query_payload(v4_url) != query_payload(diag_url):
        mismatches.append(("query_payload", query_payload(v4_url), query_payload(diag_url)))
    for key in header_keys:
        if v4_headers.get(key) != diag_headers.get(key):
            mismatches.append((f"header.{key}", v4_headers.get(key), diag_headers.get(key)))

    if mismatches:
        print("UserTweetsAndReplies parity mismatches:")
        for name, v4_value, diag_value in mismatches:
            print(f"- {name}\n  v4:   {v4_value}\n  diag: {diag_value}")
        raise SystemExit(1)

    print("UserTweetsAndReplies request parity: OK")


if __name__ == "__main__":
    main()
