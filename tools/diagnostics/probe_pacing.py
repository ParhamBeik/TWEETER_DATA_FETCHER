#!/usr/bin/env python3
"""Density-pacing probe — the one client-side lever left untested.

CONTEXT (why this exists)
  Every other client-side lever has been pulled and either shipped or disproven:
    - request context (active_user=yes + tab referer) .... SHIPPED (fixes UTAR p1)
    - warm-up ............................................. INERT / dormant
    - tx-id badge (omit/static/fake) ........................ agnostic to the gate
    - dynamic (correctly-computed) tx-id ................... no benefit (today)
    - live query-id discovery .............................. redundant (config live)
    - cursor picker / variables shape / transport .......... eliminated
  See SEARCHTIMELINE_404_ROOT_CAUSE.md + DYNAMIC_TXID_QUERYID_FINDINGS.md.

  What none of those tests ever did: send page-2+ requests at HUMAN cadence.
  Every prior run fired requests back-to-back (inter-page 0.4-0.6 s). The gate
  was therefore only ever observed under rapid density. Whether a spaced,
  jittered request sequence lets page-2+ through where rapid requests 404 is
  genuinely OPEN — this probe closes it.

WHAT IT MEASURES (single variable: inter-request delay)
  For each endpoint x target, run independent cursor chains at several delay
  regimes (rapid / steady / human+jitter). All through the SAME production code
  path (CurlCffiAPIManager + production_headers). The ONLY delta between regimes
  is the delay between successive requests in a chain.

  Primary metric: how many pages a chain reaches before the gate trips
  (max_page_reached) and whether a spaced regime goes deeper than a rapid one.

METHODOLOGY (per the prior session's warning)
  The gate is session-level and positional: whichever chain runs first can eat
  the "fresh" budget and poison later chains. To mitigate:
    1. regimes run SLOW -> FAST (the human regime gets the freshest state, so
       the "does pacing help" question gets its best possible shot);
    2. an inter-regime gap (--regime-gap) lets any short cooldown settle;
    3. every row carries the live x-rate-limit-remaining counter — a 404 that
       still decrements the counter means auth was accepted and the request was
       throttled (the soft-block signature). The per-request sequence + counter
       is the truth; aggregate success% alone can lie.

NO production code is modified. Output: tests/reports/pacing_<ts>.{json,md}.

Run:
    python tools/diagnostics/probe_pacing.py
    python tools/diagnostics/probe_pacing.py --endpoints SearchTimeline \\
        --queries OpenAI --pages 8
    python tools/diagnostics/probe_pacing.py --regimes human:9:4 steady:3:1 rapid:0.3:0
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_DIAG = Path(__file__).resolve().parent
REPO = _DIAG.parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(_DIAG))  # reuse pagination_test + probe_dynamic_txid helpers

import pagination_test as P  # noqa: E402
from probe_dynamic_txid import maybe_pace  # noqa: E402  rate-limit-aware backoff

LOG = logging.getLogger("probe_pacing")
logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")
LOG.setLevel(logging.INFO)

REPORTS = REPO / "tests" / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)

# Stop paging a chain if rate budget is exhausted to/below this (safety floor).
RATE_FLOOR = 2


# --------------------------------------------------------------------------- #
# regime parsing  "label:base_seconds:jitter_seconds"
# --------------------------------------------------------------------------- #

def parse_regimes(specs: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for spec in specs:
        label, base, jitter = spec.split(":")
        out.append({"label": label, "base": float(base), "jitter": float(jitter)})
    # SLOW -> FAST so the human regime runs on the freshest state.
    out.sort(key=lambda r: r["base"], reverse=True)
    return out


def delay_for(regime: Dict[str, Any]) -> float:
    j = regime["jitter"]
    return max(0.0, regime["base"] + (random.uniform(-j, j) if j else 0.0))


# --------------------------------------------------------------------------- #
# fire + paginate (mirrors probe_dynamic_txid.chain, minus the tx-id A/B)
# --------------------------------------------------------------------------- #

def fire(manager, endpoint, url, username, prod_ctx) -> Dict[str, Any]:
    t0 = time.time()
    try:
        resp = manager.perform_get(endpoint=endpoint, url=url, username=username, headers=dict(prod_ctx))
        status = resp.status_code
        text = getattr(resp, "text", "")
        ver = P._http_ver(resp)
        rate_rem = (getattr(resp, "headers", {}) or {}).get("x-rate-limit-remaining")
        err = None
        maybe_pace(resp)  # back off near the rate window edge
    except Exception as exc:  # noqa: BLE001
        status, text, ver, rate_rem, err = None, "", "error", None, f"{type(exc).__name__}: {exc}"
    elapsed = 1000 * (time.time() - t0)
    parsed_ok, nxt = P.parse_page(status, text, endpoint)
    return {
        "status_code": status, "response_time_ms": round(elapsed, 1),
        "http_version": ver, "parsed_ok": parsed_ok, "next_cursor": nxt,
        "rate_remaining": rate_rem, "error": err,
    }


def chain(manager, config, endpoint, qid, regime, label, user_id,
          username, raw_query, pages) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """One cursor chain at one delay regime. Returns (rows, final_rate_remaining)."""
    rows: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    last_rate: Optional[int] = None
    for page in range(1, pages + 1):
        url = P.build_url(config, endpoint, qid, user_id=user_id, cursor=cursor, raw_query=raw_query)
        prod_ctx = P.production_headers(endpoint, username, raw_query) or {}
        r = fire(manager, endpoint, url, username, prod_ctx)
        r.update({"endpoint": endpoint, "target": label, "page": page, "regime": regime["label"]})
        rows.append(r)
        LOG.info("  [%s] %s %s p%d -> %s %.0fms parsed=%s rate=%s",
                 regime["label"], endpoint, label, page, r["status_code"],
                 r["response_time_ms"], r["parsed_ok"], r["rate_remaining"])
        try:
            last_rate = int(r["rate_remaining"]) if r["rate_remaining"] is not None else last_rate
        except (TypeError, ValueError):
            pass
        if last_rate is not None and last_rate <= RATE_FLOOR:
            LOG.warning("rate budget at floor (%s); ending chain early", last_rate)
            break
        if not r["parsed_ok"]:
            break  # no cursor -> gate tripped, chain ends
        cursor = r["next_cursor"]
        if page < pages:
            d = delay_for(regime)
            rows[-1]["slept_before_next_s"] = round(d, 2)
            time.sleep(d)
    return rows, last_rate


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    buckets: Dict[tuple, list] = {}
    for r in rows:
        buckets.setdefault((r["regime"], r["endpoint"]), []).append(r)
    for key, rs in sorted(buckets.items()):
        # each (regime,endpoint) may hold several targets -> per-target max page
        targets = {}
        for r in rs:
            t = r["target"]
            targets.setdefault(t, []).append(r)
        per_target_max = {t: max((r["page"] for r in trs), default=0) for t, trs in targets.items()}
        n = len(rs)
        ok = sum(1 for r in rs if r["parsed_ok"])
        nf = sum(1 for r in rs if r["status_code"] == 404)
        times = [r["response_time_ms"] for r in rs if r["response_time_ms"] > 0]
        out["/".join(key)] = {
            "requests": n, "success": ok,
            "success_pct": round(ok / n * 100, 1) if n else 0.0,
            "404s": nf,
            "max_page_per_target": per_target_max,
            "max_page_overall": max(per_target_max.values()) if per_target_max else 0,
            "median_ms": round(statistics.median(times), 1) if times else 0,
        }
    return out


def write_report(pj: Path, pm: Path, report: Dict[str, Any]) -> None:
    pj.write_text(json.dumps(report, indent=2))
    L = [
        "# Density-Pacing Probe",
        "",
        f"- run: {report['run_started']} -> {report['run_finished']}",
        f"- params: {report['params']}",
        f"- regimes (run slow->fast): {[r['label'] for r in report['regimes']]}",
        "",
        "## Per regime x endpoint  (max_page = deepest page reached before gate)",
        "",
        "| regime/endpoint | reqs | success% | 404 | max page/target | overall max | median ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for k, m in report["summary"].items():
        L.append(
            f"| `{k}` | {m['requests']} | {m['success_pct']} | {m['404s']} | "
            f"{m['max_page_per_target']} | {m['max_page_overall']} | {m['median_ms']} |"
        )
    L += ["", "## Per-request sequence (the truth — read with the rate counter)", ""]
    for r in report["results"]:
        gate = ""
        if r["status_code"] == 404:
            gate = " [GATE: 404 still decremented rate? see rate col]"
        L.append(
            f"- `{r['regime']}` {r['endpoint']} {r['target']} p{r['page']}: "
            f"status={r['status_code']} {r['response_time_ms']}ms parsed={r['parsed_ok']} "
            f"rate={r['rate_remaining']}"
            + (f" sleep={r.get('slept_before_next_s')}s" if r.get('slept_before_next_s') else "")
            + (f" err={r['error']}" if r.get('error') else "")
            + gate
        )
    pm.write_text("\n".join(L) + "\n")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Density-pacing probe (the last untested lever)")
    ap.add_argument("--accounts", nargs="+", default=["elonmusk", "Reuters"])
    ap.add_argument("--pages", type=int, default=6)
    ap.add_argument("--endpoints", nargs="+", default=["SearchTimeline", "UserTweetsAndReplies"])
    ap.add_argument("--queries", nargs="+", default=["OpenAI"])
    ap.add_argument("--regimes", nargs="+",
                    default=["human:9:4", "steady:3:1", "rapid:0.3:0"],
                    help="label:base_sec:jitter_sec (run slow->fast)")
    ap.add_argument("--regime-gap", type=float, default=75.0,
                    help="seconds to wait between regimes (let short cooldowns settle)")
    ap.add_argument("--config", type=str)
    args = ap.parse_args()

    from tweeter_data_fetcher.configuration import resolve_config_path
    from tweeter_data_fetcher.twitter.curl_cffi_client import CurlCffiAPIManager

    cfg_path = Path(args.config) if args.config else resolve_config_path(project_root=REPO)
    config = json.loads(cfg_path.read_text())
    QID = {
        "UserTweets": config["api_config"]["user_tweets_query_id"],
        "UserTweetsAndReplies": config["api_config"]["user_tweets_and_replies_query_id"],
        "SearchTimeline": config["api_config"]["search_timeline_query_id"],
    }

    regimes = parse_regimes(args.regimes)
    LOG.info("regimes (slow->fast): %s",
             [(r["label"], r["base"], r["jitter"]) for r in regimes])

    user_ids = {a: P.resolve_user_id(cfg_path, a) for a in args.accounts}
    LOG.info("resolved: %s", user_ids)

    manager = CurlCffiAPIManager(config_path=str(cfg_path))
    started = datetime.now().isoformat()
    results: List[Dict[str, Any]] = []

    for endpoint in args.endpoints:
        qid = QID.get(endpoint, "")
        if not qid:
            LOG.warning("no query_id for %s; skipping", endpoint)
            continue
        if endpoint == "SearchTimeline":
            targets = [(q, None, None, q) for q in args.queries]
        else:
            targets = [(a, user_ids[a], a, None) for a in args.accounts if user_ids.get(a)]
            if not targets:
                LOG.warning("no resolved user_ids for %s; skipping", endpoint)
                continue
        for ri, regime in enumerate(regimes):
            if ri > 0:
                LOG.info("inter-regime gap: sleeping %.0fs", args.regime_gap)
                time.sleep(args.regime_gap)
            LOG.info("=== %s : %s (regime=%s base=%.1fs jitter=%.1fs) ===",
                     endpoint, [t[0] for t in targets], regime["label"], regime["base"], regime["jitter"])
            for label, uid, uname, rq in targets:
                rows, _ = chain(manager, config, endpoint, qid, regime,
                                label, uid, uname, rq, args.pages)
                results.extend(rows)

    report = {
        "run_started": started,
        "run_finished": datetime.now().isoformat(),
        "config_path": str(cfg_path),
        "params": vars(args),
        "regimes": regimes,
        "resolved_user_ids": user_ids,
        "query_ids": QID,
        "summary": aggregate(results),
        "results": results,
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pj = REPORTS / f"pacing_{stamp}.json"
    pm = REPORTS / f"pacing_{stamp}.md"
    write_report(pj, pm, report)
    print(f"\nJSON: {pj}\nMD:   {pm}")
    print("\n=== SUMMARY ===")
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
