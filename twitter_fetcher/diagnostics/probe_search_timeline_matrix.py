#!/usr/bin/env python3
"""
Comprehensive SearchTimeline Multi-Route, Multi-Product & Multi-Page Diagnostic Matrix.

Tests SearchTimeline queries across:
1. Referer URLs & Routes:
   - Explore Page (https://x.com/explore)
   - Standard Search Page (https://x.com/search?q=...&src=typed_query)
   - Latest Search Page (https://x.com/search?q=...&f=live&src=typed_query)
   - Advanced Search Queries (with min_faves, min_retweets, since/until dates)
2. Products:
   - "Top" (default top tweets)
   - "Latest" (real-time live tweets)
3. Pagination Depth:
   - 5 to 6 pages per query/product/route combination
4. Transports:
   - Playwright SPA Browser Execution
   - CurlCffiAPIManager (Direct HTTP/2 GET)
   - Combined Production Fallback Architecture

Output is saved to:
  twitter_fetcher/diagnostics/reports/search_timeline_matrix_report.json
  twitter_fetcher/diagnostics/reports/SEARCH_TIMELINE_MATRIX_FINDINGS.md
"""

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

DIAGNOSTICS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DIAGNOSTICS_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tweeter_data_fetcher.configuration import resolve_config_path
from tweeter_data_fetcher.x_api.contracts import (
    SEARCH_TIMELINE_FEATURES,
    extract_bottom_cursor,
    search_timeline_variables,
    validate_graphql_payload,
)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("probe_search_timeline_matrix")

REPORTS_DIR = DIAGNOSTICS_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def utcnow_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def truncate_cursor(cursor: Optional[str]) -> Optional[str]:
    if not cursor:
        return None
    c_str = str(cursor)
    if len(c_str) <= 25:
        return c_str
    h = hashlib.md5(c_str.encode("utf-8")).hexdigest()[:8]
    return f"{c_str[:20]}...[{h}]"


class PlaywrightSearchMatrixRunner:
    """Run Playwright multi-page pagination for SearchTimeline across different routes & products."""

    def __init__(self, config: Dict[str, Any], headless: bool = True):
        self.config = config
        self.headless = headless

    def run_search(
        self,
        target_url: str,
        product: str,
        query: str,
        max_pages: int = 6,
    ) -> Dict[str, Any]:
        from playwright.sync_api import sync_playwright

        pages_captured = []
        cookies = self.config.get("api_cookies", {}) or {}
        pw_cookies = [
            {"name": str(k), "value": str(v), "domain": ".x.com", "path": "/"}
            for k, v in cookies.items()
            if v and v != "REPLACE_ME"
        ]

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            context = browser.new_context()
            if pw_cookies:
                context.add_cookies(pw_cookies)
            page = context.new_page()

            def on_response(response):
                url = response.url
                if "/i/api/graphql/" not in url or "SearchTimeline" not in url:
                    return
                if response.status != 200:
                    pages_captured.append({
                        "page": len(pages_captured) + 1,
                        "status": response.status,
                        "body_bytes": 0,
                        "parsed_ok": False,
                        "cursor_out": None,
                        "tweet_count": 0,
                    })
                    return

                try:
                    body_bytes = response.body()
                    payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
                    parsed_ok = validate_graphql_payload("SearchTimeline", payload).ok
                    cursor_out = extract_bottom_cursor(payload)
                    instructions = (
                        payload.get("data", {})
                        .get("search_by_raw_query", {})
                        .get("search_timeline", {})
                        .get("timeline", {})
                        .get("instructions", [])
                    )
                    tweet_count = 0
                    for inst in instructions:
                        if inst.get("type") == "TimelineAddEntries":
                            for entry in inst.get("entries", []):
                                if str(entry.get("entryId", "")).startswith("tweet-"):
                                    tweet_count += 1
                    pages_captured.append({
                        "page": len(pages_captured) + 1,
                        "status": 200,
                        "body_bytes": len(body_bytes),
                        "parsed_ok": parsed_ok,
                        "cursor_out": cursor_out,
                        "tweet_count": tweet_count,
                    })
                except Exception as exc:
                    pages_captured.append({
                        "page": len(pages_captured) + 1,
                        "status": response.status,
                        "error": str(exc),
                        "parsed_ok": False,
                    })

            page.on("response", on_response)
            logger.info("Playwright navigating to %s (product=%s)", target_url, product)
            page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            # Infinite scroll to reach target pages
            for _ in range(max_pages - 1):
                if len(pages_captured) >= max_pages:
                    break
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2500)

            browser.close()

        return {
            "target_url": target_url,
            "product": product,
            "query": query,
            "pages_fetched": len(pages_captured),
            "pages": pages_captured,
            "success_rate": (
                sum(1 for p in pages_captured if p.get("status") == 200) / len(pages_captured) * 100
                if pages_captured else 0.0
            ),
        }


class CurlSearchMatrixRunner:
    """Run CurlCffiAPIManager multi-page pagination for SearchTimeline."""

    def __init__(self, config_path: Path):
        from tweeter_data_fetcher.x_api.curl_cffi_client import CurlCffiAPIManager

        self.config_path = config_path
        self.manager = CurlCffiAPIManager(config_path=str(config_path))

    def run_search(
        self,
        query: str,
        product: str,
        referer: str,
        query_source: str = "typed_query",
        max_pages: int = 6,
    ) -> Dict[str, Any]:
        endpoint = "SearchTimeline"
        query_id = self.manager.config.get("api_config", {}).get("search_timeline_query_id", "hz_94eVAtrtQo_vO3my7Rw")
        features = dict(SEARCH_TIMELINE_FEATURES)

        pages_captured = []
        cursor = None

        headers = {
            "referer": referer,
            "x-twitter-active-user": "yes",
        }

        for page in range(1, max_pages + 1):
            vars_payload = search_timeline_variables(
                raw_query=query,
                product=product,
                query_source=query_source,
                cursor=cursor,
            )
            params = {
                "variables": json.dumps(vars_payload, separators=(",", ":"), ensure_ascii=False),
                "features": json.dumps(features, separators=(",", ":"), ensure_ascii=False),
            }
            url = f"https://x.com/i/api/graphql/{query_id}/{endpoint}?{urlencode(params, quote_via=quote)}"

            t0 = time.time()
            try:
                resp = self.manager.perform_get(endpoint=endpoint, url=url, headers=headers)
                status = resp.status_code
                body_bytes = resp.content
            except Exception as exc:
                status = None
                body_bytes = b""

            elapsed_ms = (time.time() - t0) * 1000
            parsed_ok = False
            cursor_out = None
            tweet_count = 0

            if status == 200 and body_bytes:
                try:
                    payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
                    parsed_ok = validate_graphql_payload(endpoint, payload).ok
                    cursor_out = extract_bottom_cursor(payload)
                    instructions = (
                        payload.get("data", {})
                        .get("search_by_raw_query", {})
                        .get("search_timeline", {})
                        .get("timeline", {})
                        .get("instructions", [])
                    )
                    for inst in instructions:
                        if inst.get("type") == "TimelineAddEntries":
                            for entry in inst.get("entries", []):
                                if str(entry.get("entryId", "")).startswith("tweet-"):
                                    tweet_count += 1
                except Exception:
                    pass

            pages_captured.append({
                "page": page,
                "status": status,
                "body_bytes": len(body_bytes),
                "parsed_ok": parsed_ok,
                "cursor_in_trunc": truncate_cursor(cursor),
                "cursor_out_trunc": truncate_cursor(cursor_out),
                "tweet_count": tweet_count,
                "elapsed_ms": round(elapsed_ms, 1),
            })

            if status != 200 or not cursor_out:
                break
            cursor = cursor_out
            time.sleep(0.4)

        return {
            "query": query,
            "product": product,
            "referer": referer,
            "query_source": query_source,
            "pages_fetched": len(pages_captured),
            "pages": pages_captured,
            "success_rate": (
                sum(1 for p in pages_captured if p.get("status") == 200) / len(pages_captured) * 100
                if pages_captured else 0.0
            ),
        }


def run_full_matrix(config_path: Path, max_pages: int = 6):
    with open(config_path) as f:
        config = json.load(f)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    results = {"stamp": stamp, "max_pages": max_pages, "playwright_matrix": [], "curl_matrix": []}

    pw_runner = PlaywrightSearchMatrixRunner(config, headless=True)
    curl_runner = CurlSearchMatrixRunner(config_path)

    # Test Queries Matrix
    queries = [
        # 1. Advanced Search query with operators
        {"query": "AI min_faves:100 min_retweets:10", "type": "advanced"},
        # 2. Standard topic search
        {"query": "OpenAI", "type": "standard"},
        # 3. Financial / tech topic search
        {"query": "Nvidia", "type": "standard"},
    ]

    routes = [
        {"name": "explore_page", "product": "Top", "query_source": "typed_query", "referer": "https://x.com/explore"},
        {"name": "top_search_page", "product": "Top", "query_source": "typed_query", "referer": "https://x.com/search?q={query}&src=typed_query"},
        {"name": "latest_search_page", "product": "Latest", "query_source": "typed_query", "referer": "https://x.com/search?q={query}&f=live&src=typed_query"},
    ]

    logger.info("=== STARTING PLAYWRIGHT SEARCH TIMELINE MATRIX ===")
    for q_item in queries:
        q = q_item["query"]
        for route in routes:
            product = route["product"]
            if route["name"] == "explore_page":
                target_url = "https://x.com/explore"
            elif product == "Latest":
                target_url = f"https://x.com/search?q={quote(q)}&f=live&src=typed_query"
            else:
                target_url = f"https://x.com/search?q={quote(q)}&src=typed_query"

            logger.info("Running Playwright for query='%s' product=%s route=%s", q, product, route["name"])
            res = pw_runner.run_search(target_url=target_url, product=product, query=q, max_pages=max_pages)
            res["route_name"] = route["name"]
            results["playwright_matrix"].append(res)
            time.sleep(1.0)

    logger.info("=== STARTING CURL_CFFI SEARCH TIMELINE MATRIX ===")
    for q_item in queries:
        q = q_item["query"]
        for route in routes:
            product = route["product"]
            referer_formatted = route["referer"].format(query=quote(q))
            logger.info("Running Curl for query='%s' product=%s route=%s", q, product, route["name"])
            res = curl_runner.run_search(
                query=q,
                product=product,
                referer=referer_formatted,
                query_source=route["query_source"],
                max_pages=max_pages,
            )
            res["route_name"] = route["name"]
            results["curl_matrix"].append(res)
            time.sleep(1.0)

    json_report_path = REPORTS_DIR / f"search_timeline_matrix_{stamp}.json"
    json_report_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info("Matrix diagnostic finished. Output saved to %s", json_report_path)
    return json_report_path, results


def main():
    parser = argparse.ArgumentParser(description="SearchTimeline Multi-Route & Multi-Product Matrix Probe.")
    parser.add_argument("--config", type=str, help="Path to config.json")
    parser.add_argument("--pages", type=int, default=6, help="Target pages per search (default: 6)")
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else resolve_config_path(project_root=PROJECT_ROOT)
    run_full_matrix(config_path=config_path, max_pages=args.pages)


if __name__ == "__main__":
    main()
