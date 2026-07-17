#!/usr/bin/env python3
"""
Pagination 404 validation across all 3 production GraphQL endpoints.

This is the single essential diagnostic for the curl_cffi transport. It runs a
real multi-page pagination sweep (default 8 pages) through:
  - baseline_requests : the canonical requests-based APIManager (production)
  - curl_cffi         : the fixed CurlCffiAPIManager (no warm-up, canonical headers,
                        persistent reused HTTP/2 session)

NO warm-up on either side — this is the evidence for whether warm-up can be
dropped. Reports per-page HTTP status, latency, HTTP version, and cursor
continuity, then aggregates the page-1 vs page-2+ 404 rate that matters.

Run:
    python tools/diagnostics/pagination_test.py
    python tools/diagnostics/pagination_test.py --accounts elonmusk reuters --pages 8
    python tools/diagnostics/pagination_test.py --transport curl_cffi --pages 10
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

_DIAG = Path(__file__).resolve().parent
REPO = _DIAG.parents[1]
sys.path.insert(0, str(REPO / "src"))

from tweeter_data_fetcher.configuration import resolve_config_path
from tweeter_data_fetcher.tweets._legacy import (  # noqa: E402
    extract_bottom_cursor,
    search_timeline_variables,
    timeline_field_toggles,
    timeline_variables,
    user_by_screen_name_contract,
)

logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("pagination_test")
logger.setLevel(logging.INFO)

REPORTS = REPO / "tests" / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)

QID_KEYS = {
    "UserTweets": "user_tweets_query_id",
    "UserTweetsAndReplies": "user_tweets_and_replies_query_id",
    "SearchTimeline": "search_timeline_query_id",
    "UserByScreenName": "user_by_screen_name_query_id",
}

_ALPN = {0: "HTTP/1.0", 1: "HTTP/1.1", 2: "SPDY", 3: "HTTP/2", 4: "HTTP/3"}


# --- URL building (mirrors TimelinePaginator._build_graphql_url) ---

def _compact(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def build_url(config, endpoint, query_id, *, user_id=None, cursor=None, raw_query=None) -> str:
    payloads = config.get("graphql_endpoint_payloads", {})
    ep = payloads.get(endpoint, {}) if isinstance(payloads.get(endpoint), dict) else {}
    features = ep.get("features") if isinstance(ep.get("features"), dict) else {}
    var_cfg = ep.get("variables", {}) if isinstance(ep.get("variables"), dict) else {}

    if endpoint == "SearchTimeline":
        variables = search_timeline_variables(raw_query=raw_query or "OpenAI", product="Latest", cursor=cursor)
    else:
        template = var_cfg.get("pagination" if cursor else "initial")
        if isinstance(template, dict):
            variables = dict(template)
            variables["userId"] = user_id
            if cursor:
                variables["cursor"] = cursor
            else:
                variables.pop("cursor", None)
        else:
            variables = timeline_variables(endpoint, user_id, cursor)

    toggles = ep.get("fieldToggles")
    if not isinstance(toggles, dict) or not toggles:
        toggles = timeline_field_toggles(endpoint)

    params = {"variables": _compact(variables), "features": _compact(features)}
    if toggles:
        params["fieldToggles"] = _compact(toggles)
    return f"https://x.com/i/api/graphql/{query_id}/{endpoint}?{urlencode(params, quote_via=quote)}"


def _http_ver(resp) -> str:
    ver = getattr(resp, "http_version", "HTTP/1.1")
    return _ALPN.get(ver, str(ver)) if isinstance(ver, int) else str(ver)


def parse_page(status, text, endpoint) -> Tuple[bool, Optional[str]]:
    """Return (parsed_ok, next_cursor). parsed_ok = HTTP 200 with a clean GraphQL payload."""
    if status != 200 or not text:
        return False, None
    try:
        payload = json.loads(text)
    except Exception:
        return False, None
    if isinstance(payload, dict) and payload.get("errors"):
        return False, None
    return True, extract_bottom_cursor(payload)


# --- transports ---

def make_requests_manager(config_path):
    from tweeter_data_fetcher.twitter.client import APIManager
    return APIManager(config_path=str(config_path))


def make_curl_manager(config_path):
    from tweeter_data_fetcher.twitter.curl_cffi_client import CurlCffiAPIManager
    return CurlCffiAPIManager(config_path=str(config_path))


def resolve_user_id(config_path, username) -> Optional[str]:
    from tweeter_data_fetcher.twitter.client import APIManager
    mgr = APIManager(config_path=str(config_path))
    query_id = mgr.get_query_id("UserByScreenName")
    if not query_id:
        return None
    contract = user_by_screen_name_contract(username)
    params = {
        "variables": _compact(contract["variables"]),
        "features": _compact(contract["features"]),
        "fieldToggles": _compact(contract["fieldToggles"]),
    }
    url = f"https://x.com/i/api/graphql/{query_id}/UserByScreenName?{urlencode(params, quote_via=quote)}"
    try:
        resp = mgr.perform_get(endpoint="UserByScreenName", url=url, username=username)
    except Exception as exc:
        logger.warning("UserByScreenName @%s failed: %s", username, exc)
        return None
    if resp.status_code != 200:
        logger.warning("UserByScreenName @%s -> %s", username, resp.status_code)
        return None
    try:
        return resp.json().get("data", {}).get("user", {}).get("result", {}).get("rest_id")
    except Exception:
        return None


def production_headers(endpoint, username, raw_query) -> Optional[Dict[str, str]]:
    """Mirror exactly what the pipelines send (timeline.py:743-753, search frozen headers).

    Without this, perform_get falls back to the first context variant — for
    UserTweetsAndReplies that is `replies_tab_passive` (active_user=no), which is
    NOT what production sends (active_user=yes). Testing the wrong context
    manufactures 404s that production never hits.
    """
    if endpoint == "UserTweetsAndReplies" and username:
        return {"referer": f"https://x.com/{username}/with_replies", "x-twitter-active-user": "yes"}
    if endpoint == "UserTweets" and username:
        return {"referer": f"https://x.com/{username}", "x-twitter-active-user": "yes"}
    if endpoint == "SearchTimeline" and raw_query:
        return {"referer": f"https://x.com/search?q={quote(raw_query)}&f=live", "x-twitter-active-user": "yes"}
    return None


def run_transport(name, manager, config, query_ids, endpoint, targets, pages, inter_page_sleep):
    """targets: list of (label, user_id, username_or_None, raw_query_or_None)."""
    results: List[Dict[str, Any]] = []
    for label, user_id, username, raw_query in targets:
        cursor = None
        for page in range(1, pages + 1):
            url = build_url(
                config, endpoint, query_ids[endpoint],
                user_id=user_id, cursor=cursor, raw_query=raw_query,
            )
            t0 = time.time()
            try:
                resp = manager.perform_get(
                    endpoint=endpoint, url=url, username=username,
                    headers=production_headers(endpoint, username, raw_query),
                )
                status = resp.status_code
                text = getattr(resp, "text", "")
                ver = _http_ver(resp)
                err = None
            except Exception as exc:  # noqa: BLE001
                status, text, ver, err = None, "", "error", f"{type(exc).__name__}: {exc}"
            elapsed = (time.time() - t0) * 1000
            parsed_ok, nxt = parse_page(status, text, endpoint)
            results.append({
                "transport": name, "endpoint": endpoint, "target": label, "page": page,
                "status_code": status, "response_time_ms": round(elapsed, 1),
                "http_version": ver, "parsed_ok": parsed_ok,
                "cursor_advanced": bool(nxt) if parsed_ok else None,
                "error": err,
            })
            logger.info(
                "  [%s] %s %s p%d -> %s %.0fms %s parsed=%s",
                name, endpoint, label, page, status, elapsed, ver, parsed_ok,
            )
            if not parsed_ok:
                break  # no cursor → stop this target
            cursor = nxt
            if page < pages:
                time.sleep(inter_page_sleep)
    return results


# --- reporting ---

def aggregate(results):
    out = {}
    for key, rs in _group(results, lambda r: (r["transport"], r["endpoint"])):
        n = len(rs)
        ok = sum(1 for r in rs if r["parsed_ok"])
        not_found = sum(1 for r in rs if r["status_code"] == 404)
        times = [r["response_time_ms"] for r in rs if r["response_time_ms"] > 0]
        page1 = [r for r in rs if r["page"] == 1]
        page2plus = [r for r in rs if r["page"] >= 2]
        out["/".join(key)] = {
            "requests": n,
            "success": ok,
            "success_pct": round(ok / n * 100, 1) if n else 0.0,
            "404s": not_found,
            "404_pct": round(not_found / n * 100, 1) if n else 0.0,
            "page1_404s": sum(1 for r in page1 if r["status_code"] == 404),
            "page2plus_404s": sum(1 for r in page2plus if r["status_code"] == 404),
            "page2plus_total": len(page2plus),
            "avg_ms": round(statistics.mean(times), 1) if times else 0,
            "median_ms": round(statistics.median(times), 1) if times else 0,
            "http_versions": dict(_count([r["http_version"] for r in rs])),
        }
    return out


def _group(items, keyfn):
    buckets: Dict[tuple, list] = {}
    for it in items:
        buckets.setdefault(keyfn(it), []).append(it)
    return buckets.items()


def _count(seq):
    d: Dict[str, int] = {}
    for s in seq:
        d[str(s)] = d.get(str(s), 0) + 1
    return d


def write_report(path_json, path_md, report):
    with open(path_json, "w") as f:
        json.dump(report, f, indent=2)
    lines = [
        "# Pagination 404 Validation",
        "",
        f"- run: {report['run_started']} → {report['run_finished']}",
        f"- params: {report['params']}",
        f"- query_ids: `{report['query_ids']}`",
        f"- resolved user_ids: {report['resolved_user_ids']}",
        "",
        "## Per transport × endpoint",
        "",
        "| transport/endpoint | reqs | success% | 404s | 404% | p1 404 | p2+ 404 | p2+ total | median ms |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for k, m in report["summary"].items():
        lines.append(
            f"| `{k}` | {m['requests']} | {m['success_pct']} | {m['404s']} | {m['404_pct']} | "
            f"{m['page1_404s']} | {m['page2plus_404s']} | {m['page2plus_total']} | {m['median_ms']} |"
        )
    lines += ["", "## Per-request detail", ""]
    for r in report["results"]:
        lines.append(
            f"- `{r['transport']}` {r['endpoint']} {r['target']} p{r['page']}: "
            f"status={r['status_code']} {r['response_time_ms']}ms {r['http_version']} "
            f"parsed={r['parsed_ok']}" + (f" err={r['error']}" if r.get("error") else "")
        )
    path_md.write_text("\n".join(lines) + "\n")


# --- main ---

def main():
    p = argparse.ArgumentParser(description="Pagination 404 validation across all 3 endpoints")
    p.add_argument("--accounts", nargs="+", default=["elonmusk", "reuters"])
    p.add_argument("--pages", type=int, default=8)
    p.add_argument("--endpoints", nargs="+", default=["UserTweets", "UserTweetsAndReplies", "SearchTimeline"])
    p.add_argument("--transport", choices=["requests", "curl_cffi", "both"], default="both")
    p.add_argument("--search-query", default="OpenAI")
    p.add_argument("--inter-page-sleep", type=float, default=0.4)
    p.add_argument("--config", type=str)
    args = p.parse_args()

    config_path = Path(args.config) if args.config else resolve_config_path(project_root=REPO)
    with open(config_path) as f:
        config = json.load(f)
    api_config = config.get("api_config", {})
    query_ids = {ep: api_config.get(k, "") for ep, k in QID_KEYS.items()}
    logger.info("query_ids: %s", {k: v for k, v in query_ids.items() if v})

    user_ids = {}
    for acct in args.accounts:
        uid = resolve_user_id(config_path, acct)
        logger.info("resolved @%s -> %s", acct, uid)
        user_ids[acct] = uid

    transports = []
    if args.transport in ("requests", "both"):
        transports.append(("requests", make_requests_manager(config_path)))
    if args.transport in ("curl_cffi", "both"):
        transports.append(("curl_cffi", make_curl_manager(config_path)))

    all_results: List[Dict[str, Any]] = []
    started = datetime.now().isoformat()

    for endpoint in args.endpoints:
        qid = query_ids.get(endpoint, "")
        if not qid:
            logger.warning("no query_id for %s; skipping", endpoint)
            continue
        if endpoint == "SearchTimeline":
            targets = [(args.search_query, None, None, args.search_query)]
        else:
            targets = [(acct, user_ids.get(acct), acct, None) for acct in args.accounts if user_ids.get(acct)]
            if not targets:
                logger.warning("no resolved user_ids for %s; skipping", endpoint)
                continue
        for name, manager in transports:
            logger.info("=== %s : %s (%d target(s), %d page(s)) ===", name, endpoint, len(targets), args.pages)
            all_results.extend(
                run_transport(name, manager, config, query_ids, endpoint, targets, args.pages, args.inter_page_sleep)
            )

    report = {
        "run_started": started,
        "run_finished": datetime.now().isoformat(),
        "config_path": str(config_path),
        "params": {
            "accounts": args.accounts, "pages": args.pages, "endpoints": args.endpoints,
            "transport": args.transport, "search_query": args.search_query,
            "inter_page_sleep": args.inter_page_sleep,
        },
        "resolved_user_ids": user_ids,
        "query_ids": query_ids,
        "summary": aggregate(all_results),
        "results": all_results,
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pj = REPORTS / f"pagination_test_{stamp}.json"
    pm = REPORTS / f"pagination_test_{stamp}.md"
    write_report(pj, pm, report)
    print(f"\nJSON: {pj}\nMD:   {pm}")
    print("\n=== SUMMARY ===")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
