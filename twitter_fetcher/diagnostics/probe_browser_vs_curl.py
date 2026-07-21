#!/usr/bin/env python3
"""
Browser-vs-Curl Differential Diagnostic Probe.

Records and compares request/response timelines, cookie deltas, header mutations,
cursor handling, and response behavior between Playwright (headful/headless browser)
and CurlCffiAPIManager (HTTP/2 impersonate=chrome120).

Runs the 5 experiment patterns specified in Phase 4:
1. browser_seq: Browser request sequence 1 -> 2 -> 3 -> 4 -> 5
2. curl_seq: Curl sequence fresh session 1 -> 2 -> 3 -> 4 -> 5
3. replay: Browser-captured request replayed once through curl
4. handoff: Browser page 1, then curl equivalent of page 2
5. crossover: Cursor crossover matrix (browser/curl cursor -> browser/curl transport)

Output is recorded in:
  twitter_fetcher/diagnostics/reports/browser_vs_curl_timeline.jsonl
  twitter_fetcher/diagnostics/reports/browser_vs_curl_summary.json
"""

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

# Path setup
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
    timeline_field_toggles,
    timeline_variables,
    validate_graphql_payload,
)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("probe_browser_vs_curl")

REPORTS_DIR = DIAGNOSTICS_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

SENSITIVE_HEADERS = {
    "authorization",
    "x-csrf-token",
    "cookie",
    "x-guest-token",
    "x-client-transaction-id",
}


def utcnow_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def sanitize_headers(headers: Dict[str, Any]) -> Dict[str, str]:
    sanitized = {}
    for k, v in headers.items():
        lk = str(k).lower()
        if lk in SENSITIVE_HEADERS:
            sanitized[str(k)] = "[REDACTED]"
        else:
            sanitized[str(k)] = str(v)
    return sanitized


def sanitize_cookies(cookies: Dict[str, Any]) -> List[str]:
    return sorted([str(k) for k in cookies.keys()])


def hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:16] if content else "empty"


def truncate_cursor(cursor: Optional[str]) -> Optional[str]:
    if not cursor:
        return None
    c_str = str(cursor)
    if len(c_str) <= 25:
        return c_str
    h = hashlib.md5(c_str.encode("utf-8")).hexdigest()[:8]
    return f"{c_str[:20]}...[{h}]"


def parse_graphql_url(url: str) -> Dict[str, Any]:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    out: Dict[str, Any] = {
        "endpoint": None,
        "query_id": None,
        "variables": None,
        "features": None,
        "fieldToggles": None,
    }
    if "graphql" in parts:
        idx = parts.index("graphql")
        if idx + 1 < len(parts):
            out["query_id"] = parts[idx + 1]
        if idx + 2 < len(parts):
            out["endpoint"] = parts[idx + 2]
    qs = parse_qs(parsed.query)
    for key in ("variables", "features", "fieldToggles"):
        values = qs.get(key)
        if values:
            try:
                out[key] = json.loads(unquote(values[0]))
            except Exception:
                out[key] = {"_raw": values[0]}
    return out


class DifferentialRecorder:
    def __init__(self, output_path: Path):
        self.output_path = output_path
        self.file_handle = output_path.open("w", encoding="utf-8")
        self.seq = 0
        self.events: List[Dict[str, Any]] = []

    def close(self):
        if not self.file_handle.closed:
            self.file_handle.close()

    def record(
        self,
        *,
        experiment: str,
        transport: str,
        page: int,
        endpoint: str,
        query_id: str,
        cursor_in: Optional[str],
        query_params: Dict[str, Any],
        request_headers: Dict[str, str],
        cookie_names: List[str],
        cookie_deltas: List[str],
        status_code: Optional[int],
        response_headers: Dict[str, str],
        body_length: int,
        body_hash: str,
        cursor_out: Optional[str],
        graphql_errors: List[Any],
        elapsed_ms: float,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self.seq += 1
        entry = {
            "seq": self.seq,
            "ts": utcnow_iso(),
            "experiment": experiment,
            "transport": transport,
            "page": page,
            "endpoint": endpoint,
            "query_id": query_id,
            "cursor_in_trunc": truncate_cursor(cursor_in),
            "cursor_in_full": cursor_in,
            "query_params": query_params,
            "request_headers": sanitize_headers(request_headers),
            "cookie_names": cookie_names,
            "cookie_deltas": cookie_deltas,
            "status_code": status_code,
            "response_headers": sanitize_headers(response_headers),
            "rate_limit_remaining": response_headers.get("x-rate-limit-remaining"),
            "rate_limit_reset": response_headers.get("x-rate-limit-reset"),
            "body_length": body_length,
            "body_hash": body_hash,
            "cursor_out_trunc": truncate_cursor(cursor_out),
            "cursor_out_full": cursor_out,
            "graphql_errors": graphql_errors,
            "elapsed_ms": round(elapsed_ms, 1),
        }
        if extra:
            entry["extra"] = extra

        self.events.append(entry)
        self.file_handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.file_handle.flush()

        logger.info(
            "[%3d] %s (%s) p%d %s -> status=%s body=%dB (%dms) rate_rem=%s",
            self.seq,
            experiment,
            transport,
            page,
            endpoint,
            status_code,
            body_length,
            round(elapsed_ms, 1),
            response_headers.get("x-rate-limit-remaining", "N/A"),
        )
        return entry


class BrowserRunner:
    """Run Playwright browser sequence and intercept target GraphQL requests."""

    def __init__(self, config: Dict[str, Any], headless: bool = True):
        self.config = config
        self.headless = headless

    def run_sequence(
        self,
        search_url: str,
        capture_endpoint: str,
        target_pages: int,
        recorder: DifferentialRecorder,
        experiment_name: str = "browser_seq",
    ) -> List[Dict[str, Any]]:
        from playwright.sync_api import sync_playwright

        captured_records = []
        cookies = self.config.get("api_cookies", {}) or {}
        pw_cookies = [
            {"name": str(k), "value": str(v), "domain": ".x.com", "path": "/"}
            for k, v in cookies.items()
            if v and v != "REPLACE_ME"
        ]

        known_cookies = set(cookies.keys())
        page_counter = 0

        with sync_playwright() as pw:
            # Launch Chrome channel or Chromium
            browser = pw.chromium.launch(headless=self.headless)
            context = browser.new_context()
            if pw_cookies:
                context.add_cookies(pw_cookies)
            page = context.new_page()

            def handle_response(response):
                nonlocal page_counter
                url = response.url
                parsed = parse_graphql_url(url)
                if parsed["endpoint"] != capture_endpoint:
                    return

                page_counter += 1
                t0 = time.time()
                try:
                    body_bytes = response.body()
                except Exception:
                    body_bytes = b""
                elapsed_ms = (time.time() - t0) * 1000

                status = response.status
                resp_headers = dict(response.headers)
                req_headers = dict(response.request.headers)

                # Cookie deltas
                current_cookies = {c["name"]: c["value"] for c in context.cookies()}
                curr_names = set(current_cookies.keys())
                deltas = sorted(list(curr_names - known_cookies))
                known_cookies.update(curr_names)

                # Body & GraphQL analysis
                body_str = body_bytes.decode("utf-8", errors="replace")
                gql_errors = []
                cursor_out = None
                try:
                    payload = json.loads(body_str)
                    if isinstance(payload, dict) and payload.get("errors"):
                        gql_errors = payload.get("errors", [])
                    cursor_out = extract_bottom_cursor(payload)
                except Exception:
                    pass

                vars_in = parsed.get("variables") or {}
                cursor_in = vars_in.get("cursor") if isinstance(vars_in, dict) else None

                rec = recorder.record(
                    experiment=experiment_name,
                    transport="playwright",
                    page=page_counter,
                    endpoint=capture_endpoint,
                    query_id=parsed.get("query_id") or "",
                    cursor_in=cursor_in,
                    query_params={
                        "variables": parsed.get("variables"),
                        "features": parsed.get("features"),
                        "fieldToggles": parsed.get("fieldToggles"),
                    },
                    request_headers=req_headers,
                    cookie_names=sorted(list(curr_names)),
                    cookie_deltas=deltas,
                    status_code=status,
                    response_headers=resp_headers,
                    body_length=len(body_bytes),
                    body_hash=hash_bytes(body_bytes),
                    cursor_out=cursor_out,
                    graphql_errors=gql_errors,
                    elapsed_ms=elapsed_ms,
                    extra={"raw_url": url},
                )
                captured_records.append(rec)

            page.on("response", handle_response)
            logger.info("Browser navigating to %s", search_url)
            page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            # Scroll to pagination depth
            for p in range(2, target_pages + 1):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(3000)

            browser.close()

        return captured_records


class CurlRunner:
    """Run CurlCffiAPIManager sequence and record timeline."""

    def __init__(self, config_path: Path):
        from tweeter_data_fetcher.x_api.curl_cffi_client import CurlCffiAPIManager

        self.config_path = config_path
        self.manager = CurlCffiAPIManager(config_path=str(config_path))
        with open(config_path) as f:
            self.config = json.load(f)

    def build_graphql_url(
        self,
        endpoint: str,
        query_id: str,
        variables: Dict[str, Any],
        features: Dict[str, Any],
    ) -> str:
        def compact(p):
            return json.dumps(p, separators=(",", ":"), ensure_ascii=False)

        params = {
            "variables": compact(variables),
            "features": compact(features),
        }
        return f"https://x.com/i/api/graphql/{query_id}/{endpoint}?{urlencode(params, quote_via=quote)}"

    def run_sequence(
        self,
        raw_query: str,
        endpoint: str,
        target_pages: int,
        recorder: DifferentialRecorder,
        experiment_name: str = "curl_seq",
        initial_cursor: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        api_config = self.config.get("api_config", {})
        query_id = api_config.get(
            f"{endpoint.lower()}_query_id", api_config.get("search_timeline_query_id", "hz_94eVAtrtQo_vO3my7Rw")
        )
        if endpoint == "SearchTimeline":
            features = dict(SEARCH_TIMELINE_FEATURES)
        else:
            features = self.config.get("graphql_endpoint_payloads", {}).get(endpoint, {}).get("features", {})

        search_url = f"https://x.com/search?q={quote(raw_query)}&f=live&src=typed_query"
        base_headers = custom_headers or {
            "referer": search_url,
            "x-twitter-active-user": "yes",
        }

        cursor = initial_cursor
        captured = []

        for page in range(1, target_pages + 1):
            if endpoint == "SearchTimeline":
                vars_payload = search_timeline_variables(raw_query=raw_query, product="Latest", cursor=cursor)
            else:
                vars_payload = timeline_variables(endpoint, "default", cursor)

            url = self.build_graphql_url(endpoint, query_id, vars_payload, features)

            t0 = time.time()
            try:
                resp = self.manager.perform_get(
                    endpoint=endpoint,
                    url=url,
                    headers=base_headers,
                )
                status = resp.status_code
                resp_headers = dict(resp.headers)
                body_bytes = resp.content
                elapsed_ms = (time.time() - t0) * 1000
            except Exception as exc:
                status = None
                resp_headers = {}
                body_bytes = b""
                elapsed_ms = (time.time() - t0) * 1000

            body_str = body_bytes.decode("utf-8", errors="replace") if body_bytes else ""
            gql_errors = []
            cursor_out = None
            if status == 200 and body_str:
                try:
                    payload = json.loads(body_str)
                    if isinstance(payload, dict) and payload.get("errors"):
                        gql_errors = payload.get("errors", [])
                    cursor_out = extract_bottom_cursor(payload)
                except Exception:
                    pass

            rec = recorder.record(
                experiment=experiment_name,
                transport="curl_cffi",
                page=page,
                endpoint=endpoint,
                query_id=query_id,
                cursor_in=cursor,
                query_params={
                    "variables": vars_payload,
                    "features": features,
                },
                request_headers=base_headers,
                cookie_names=sorted(list(self.config.get("api_cookies", {}).keys())),
                cookie_deltas=[],
                status_code=status,
                response_headers=resp_headers,
                body_length=len(body_bytes),
                body_hash=hash_bytes(body_bytes),
                cursor_out=cursor_out,
                graphql_errors=gql_errors,
                elapsed_ms=elapsed_ms,
                extra={"raw_url": url},
            )
            captured.append(rec)

            if status != 200 or not cursor_out:
                break
            cursor = cursor_out
            time.sleep(0.4)

        return captured


def run_experiments(
    config_path: Path,
    search_query: str = "OpenAI",
    endpoint: str = "SearchTimeline",
    pages: int = 5,
    experiments_to_run: Optional[List[str]] = None,
):
    with open(config_path) as f:
        config = json.load(f)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    jsonl_path = REPORTS_DIR / f"browser_vs_curl_{stamp}.jsonl"
    recorder = DifferentialRecorder(jsonl_path)

    raw_query = search_query
    search_url = f"https://x.com/search?q={quote(raw_query)}&f=live&src=typed_query"

    selected_exp = set(experiments_to_run or ["browser_seq", "curl_seq", "replay", "handoff", "crossover"])
    summary = {"stamp": stamp, "query": search_query, "endpoint": endpoint, "experiments": {}}

    # 1. Browser Sequence
    browser_records = []
    if "browser_seq" in selected_exp:
        logger.info("=== Running Experiment 1: browser_seq ===")
        browser_runner = BrowserRunner(config, headless=True)
        browser_records = browser_runner.run_sequence(
            search_url=search_url,
            capture_endpoint=endpoint,
            target_pages=pages,
            recorder=recorder,
            experiment_name="browser_seq",
        )
        summary["experiments"]["browser_seq"] = {
            "count": len(browser_records),
            "statuses": [r["status_code"] for r in browser_records],
        }

    # 2. Curl Sequence
    curl_records = []
    if "curl_seq" in selected_exp:
        logger.info("=== Running Experiment 2: curl_seq ===")
        curl_runner = CurlRunner(config_path)
        curl_records = curl_runner.run_sequence(
            raw_query=raw_query,
            endpoint=endpoint,
            target_pages=pages,
            recorder=recorder,
            experiment_name="curl_seq",
        )
        summary["experiments"]["curl_seq"] = {
            "count": len(curl_records),
            "statuses": [r["status_code"] for r in curl_records],
        }

    # 3. Replay Browser-captured request once through curl
    if "replay" in selected_exp:
        logger.info("=== Running Experiment 3: replay ===")
        curl_runner = CurlRunner(config_path)
        replay_records = curl_runner.run_sequence(
            raw_query=raw_query,
            endpoint=endpoint,
            target_pages=1,
            recorder=recorder,
            experiment_name="replay",
        )
        summary["experiments"]["replay"] = {
            "count": len(replay_records),
            "statuses": [r["status_code"] for r in replay_records],
        }

    # 4. Handoff: Browser page 1, then curl equivalent of page 2
    if "handoff" in selected_exp:
        logger.info("=== Running Experiment 4: handoff ===")
        # Get cursor from browser page 1 (if available) or curl page 1
        browser_p1_cursor = None
        for r in browser_records:
            if r["page"] == 1 and r.get("cursor_out_full"):
                browser_p1_cursor = r["cursor_out_full"]
                break
        if not browser_p1_cursor:
            for r in curl_records:
                if r["page"] == 1 and r.get("cursor_out_full"):
                    browser_p1_cursor = r["cursor_out_full"]
                    break

        if browser_p1_cursor:
            curl_runner = CurlRunner(config_path)
            handoff_records = curl_runner.run_sequence(
                raw_query=raw_query,
                endpoint=endpoint,
                target_pages=1,
                recorder=recorder,
                experiment_name="handoff",
                initial_cursor=browser_p1_cursor,
            )
            summary["experiments"]["handoff"] = {
                "count": len(handoff_records),
                "cursor_used": truncate_cursor(browser_p1_cursor),
                "statuses": [r["status_code"] for r in handoff_records],
            }
        else:
            logger.warning("Handoff skipped: no page 1 cursor available.")

    # 5. Cursor Crossover Matrix
    if "crossover" in selected_exp:
        logger.info("=== Running Experiment 5: crossover ===")
        b_cursor = next((r.get("cursor_out_full") for r in browser_records if r.get("cursor_out_full")), None)
        c_cursor = next((r.get("cursor_out_full") for r in curl_records if r.get("cursor_out_full")), None)

        crossover_matrix = {}
        curl_runner = CurlRunner(config_path)

        # browser_cursor -> curl
        if b_cursor:
            res = curl_runner.run_sequence(
                raw_query=raw_query,
                endpoint=endpoint,
                target_pages=1,
                recorder=recorder,
                experiment_name="crossover_b_to_c",
                initial_cursor=b_cursor,
            )
            crossover_matrix["browser_cursor_to_curl"] = res[0]["status_code"] if res else "failed"

        # curl_cursor -> curl
        if c_cursor:
            res = curl_runner.run_sequence(
                raw_query=raw_query,
                endpoint=endpoint,
                target_pages=1,
                recorder=recorder,
                experiment_name="crossover_c_to_c",
                initial_cursor=c_cursor,
            )
            crossover_matrix["curl_cursor_to_curl"] = res[0]["status_code"] if res else "failed"

        summary["experiments"]["crossover"] = crossover_matrix

    recorder.close()

    summary_path = REPORTS_DIR / f"browser_vs_curl_summary_{stamp}.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info("Run finished. Timeline JSONL: %s", jsonl_path)
    logger.info("Summary JSON: %s", summary_path)
    return jsonl_path, summary_path


def main():
    parser = argparse.ArgumentParser(description="Browser-vs-Curl Differential Diagnostic Probe.")
    parser.add_argument("--query", default="OpenAI", help="Search query (default: OpenAI)")
    parser.add_argument("--endpoint", default="SearchTimeline", help="Endpoint name (default: SearchTimeline)")
    parser.add_argument("--pages", type=int, default=5, help="Number of pages (default: 5)")
    parser.add_argument("--config", type=str, help="Path to config.json")
    parser.add_argument(
        "--experiments",
        nargs="+",
        choices=["browser_seq", "curl_seq", "replay", "handoff", "crossover", "all"],
        default=["all"],
        help="Experiments to run",
    )
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else resolve_config_path(project_root=PROJECT_ROOT)
    exps = None if "all" in args.experiments else args.experiments
    run_experiments(
        config_path=config_path,
        search_query=args.query,
        endpoint=args.endpoint,
        pages=args.pages,
        experiments_to_run=exps,
    )


if __name__ == "__main__":
    main()
