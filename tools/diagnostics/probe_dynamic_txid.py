#!/usr/bin/env python3
"""Hypothesis probe: does a CORRECTLY-COMPUTED, per-request x-client-transaction-id
change behaviour vs. the production static pool, across the 3 GraphQL endpoints?

CONTEXT (why this exists)
  Prior diagnostics (probe_txid.py, pagination_test.py, SEARCHTIMELINE_404_ROOT_CAUSE.md)
  proved:
    - the tx-id *badge* is not the gate *by itself*: fake / random / static all behave
      the same while auth is accepted;
    - warm-up is inert; UserTweetsAndReplies page-1 404 was a context bug (fixed);
    - SearchTimeline page-2+ 404 is a server-side anti-abuse gate (both clients get it).
  BUT none of those tests ever sent a tx-id computed by X's *actual algorithm*
  (path + time + ondemand-animation key). The static pools are size 1; a real browser
  emits a fresh, path-specific value on every request. This probe closes that seam.

WHAT IT MEASURES (single-variable A/B through the SAME production code path)
    A = control:   CurlCffiAPIManager.perform_get(...), production headers, STATIC
                    pool tx-id (what production sends today) — no override.
    B = dynamic:   identical, EXCEPT x-client-transaction-id is overridden with a
                    per-request value computed via xclienttransaction
                    (ClientTransaction.generate_transaction_id(method, path)).
  Every other header is byte-identical (both route through
  APIManager._build_request_headers). The ONLY delta is the tx-id value.
  Each variant paginates its OWN cursor chain (A and B never share a cursor).

ENDPOINTS
    UserTweets, UserTweetsAndReplies : per-target-account pagination.
    SearchTimeline                  : per-query pagination (the gate hotspot).

RATE SAFETY
  UserTweets / SearchTimeline are 50 req / 15 min per session (shared across all
  targets). We pace off the live `x-rate-limit-remaining` / `x-rate-limit-reset`
  headers to avoid 429s contaminating the A/B comparison.

NO production code is modified. Output: tests/reports/dynamic_txid_<ts>.{json,md}.

Run:
    python tools/diagnostics/probe_dynamic_txid.py
    python tools/diagnostics/probe_dynamic_txid.py --accounts elonmusk Reuters \\
        --pages 8 --queries OpenAI Bitcoin
    python tools/diagnostics/probe_dynamic_txid.py --endpoints SearchTimeline \\
        --queries OpenAI --pages 10
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
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

_DIAG = Path(__file__).resolve().parent
REPO = _DIAG.parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(_DIAG))  # reuse pagination_test helpers

import pagination_test as P  # noqa: E402

LOG = logging.getLogger("probe_dynamic_txid")
logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")
LOG.setLevel(logging.INFO)

REPORTS = REPO / "tests" / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# x-rate-limit safety: stop paging a chain if remaining drops to/below this.
RATE_FLOOR = 2


# --------------------------------------------------------------------------- #
# tx-id generator (seeded once per run from the logged-in home page)
# --------------------------------------------------------------------------- #

class TxGenerator:
    """Wraps xclienttransaction.ClientTransaction, seeded from X's live bundles.

    A real browser computes x-client-transaction-id from (a) the twitter-site-
    verification key + loading-x-anim frames in the home HTML and (b) key-byte
    indices in the ondemand.s JS. Both are only present on the LOGGED-IN home
    page (the logged-out page has no chunk map), so we fetch /home with cookies.
    """

    def __init__(self) -> None:
        self.ct = None
        self.seed_error: Optional[str] = None
        self.meta: Dict[str, Any] = {}

    def seed(self, cookies: Dict[str, str]) -> bool:
        from bs4 import BeautifulSoup
        from curl_cffi import requests as cffi
        from x_client_transaction import ClientTransaction
        from x_client_transaction.utils import get_ondemand_file_url

        sess = cffi.Session(impersonate="chrome120")
        for k, v in cookies.items():
            if v:
                sess.cookies.set(k, str(v), domain=".x.com")
        try:
            t0 = time.time()
            home = sess.get("https://x.com/home", headers={"user-agent": UA}, timeout=25)
            home_bs = BeautifulSoup(home.text, "html.parser")
            od_url = get_ondemand_file_url(home_bs)
            od = sess.get(od_url, headers={"user-agent": UA, "referer": "https://x.com/"}, timeout=25)
            self.ct = ClientTransaction(home_bs, od.text)
            self.meta = {
                "home_status": home.status_code, "home_bytes": len(home.text),
                "ondemand_url": od_url, "ondemand_status": od.status_code,
                "seed_ms": round(1000 * (time.time() - t0), 0),
            }
            LOG.info("tx generator seeded from live bundles (%sms)", self.meta["seed_ms"])
            return True
        except Exception as exc:  # noqa: BLE001
            self.seed_error = f"{type(exc).__name__}: {exc}"
            LOG.error("tx generator seed FAILED: %s", self.seed_error)
            return False

    def tid_for(self, url: str) -> Optional[str]:
        if self.ct is None:
            return None
        parts = urlparse(url)
        path = parts.path + ("?" + parts.query if parts.query else "")
        return self.ct.generate_transaction_id("GET", path)


# --------------------------------------------------------------------------- #
# rate-limit-aware pacing
# --------------------------------------------------------------------------- #

def maybe_pace(resp, floor: int = RATE_FLOOR) -> Optional[float]:
    """If x-rate-limit-remaining is at/below floor, sleep until reset. Return slept s."""
    headers = getattr(resp, "headers", {}) or {}
    remaining = headers.get("x-rate-limit-remaining")
    reset = headers.get("x-rate-limit-reset")
    if remaining is None:
        return None
    try:
        rem = int(remaining)
    except ValueError:
        return None
    if rem > floor or not reset:
        return None
    try:
        wait = max(0.0, int(reset) - time.time() + 1.0)
    except ValueError:
        return None
    wait = min(wait, 920.0)  # never sleep past the 15-min window
    if wait > 0:
        LOG.warning("rate-remaining=%s at floor; sleeping %.0fs until reset", rem, wait)
        time.sleep(wait)
        return wait
    return None


# --------------------------------------------------------------------------- #
# A/B fire + paginate
# --------------------------------------------------------------------------- #

def fire(manager, endpoint, url, username, prod_ctx, txgen, variant) -> Dict[str, Any]:
    headers = dict(prod_ctx)
    dyn_tid = None
    if variant == "B":
        dyn_tid = txgen.tid_for(url)
        if dyn_tid:
            headers["x-client-transaction-id"] = dyn_tid
    t0 = time.time()
    try:
        resp = manager.perform_get(endpoint=endpoint, url=url, username=username, headers=headers)
        status = resp.status_code
        text = getattr(resp, "text", "")
        ver = P._http_ver(resp)
        rate_rem = (getattr(resp, "headers", {}) or {}).get("x-rate-limit-remaining")
        err = None
        maybe_pace(resp)
    except Exception as exc:  # noqa: BLE001
        status, text, ver, rate_rem, err = None, "", "error", None, f"{type(exc).__name__}: {exc}"
    elapsed = 1000 * (time.time() - t0)
    parsed_ok, nxt = P.parse_page(status, text, endpoint)
    return {
        "status_code": status, "response_time_ms": round(elapsed, 1),
        "http_version": ver, "parsed_ok": parsed_ok, "next_cursor": nxt,
        "rate_remaining": rate_rem,
        "txid_len": len(dyn_tid) if dyn_tid else 0,
        "txid_source": "dynamic" if (variant == "B" and dyn_tid) else "static_pool",
        "error": err,
    }


def chain(manager, txgen, variant, config, endpoint, qid, label, user_id,
          username, raw_query, pages, inter_page) -> List[Dict[str, Any]]:
    """One independent cursor chain for one variant on one target."""
    rows: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    for page in range(1, pages + 1):
        url = P.build_url(config, endpoint, qid, user_id=user_id, cursor=cursor, raw_query=raw_query)
        prod_ctx = P.production_headers(endpoint, username, raw_query) or {}
        r = fire(manager, endpoint, url, username, prod_ctx, txgen, variant)
        r.update({"variant": variant, "endpoint": endpoint, "target": label, "page": page})
        rows.append(r)
        LOG.info("  [%s] %s %s p%d -> %s %.0fms parsed=%s rate=%s",
                 variant, endpoint, label, page, r["status_code"],
                 r["response_time_ms"], r["parsed_ok"], r["rate_remaining"])
        if not r["parsed_ok"]:
            break  # no cursor → chain ends (this is the 404-gate signal)
        cursor = r["next_cursor"]
        if page < pages:
            time.sleep(inter_page)
    return rows


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    buckets: Dict[tuple, list] = {}
    for r in rows:
        buckets.setdefault((r["variant"], r["endpoint"]), []).append(r)
    for key, rs in sorted(buckets.items()):
        n = len(rs)
        ok = sum(1 for r in rs if r["parsed_ok"])
        nf = sum(1 for r in rs if r["status_code"] == 404)
        rl = sum(1 for r in rs if r["status_code"] == 429)
        times = [r["response_time_ms"] for r in rs if r["response_time_ms"] > 0]
        p1 = [r for r in rs if r["page"] == 1]
        p2 = [r for r in rs if r["page"] >= 2]
        # how many chains reached each page-depth
        maxpage = max((r["page"] for r in rs), default=0)
        out["/".join(key)] = {
            "requests": n, "success": ok,
            "success_pct": round(ok / n * 100, 1) if n else 0.0,
            "404s": nf, "429s": rl,
            "page1_200": sum(1 for r in p1 if r["parsed_ok"]),
            "page2plus_total": len(p2),
            "page2plus_success": sum(1 for r in p2 if r["parsed_ok"]),
            "max_page_reached": maxpage,
            "avg_ms": round(statistics.mean(times), 1) if times else 0,
            "median_ms": round(statistics.median(times), 1) if times else 0,
        }
    return out


def write_report(pj: Path, pm: Path, report: Dict[str, Any]) -> None:
    pj.write_text(json.dumps(report, indent=2))
    L = [
        "# Dynamic x-client-transaction-id Hypothesis Probe",
        "",
        f"- run: {report['run_started']} → {report['run_finished']}",
        f"- tx generator: `{report['tx_seed']}`",
        f"- params: {report['params']}",
        "",
        "## A vs B per endpoint  (A=static pool · B=dynamic per-request tx-id)",
        "",
        "| variant/endpoint | reqs | success% | 404 | 429 | p1 200 | p2+ total | p2+ success | max page | median ms |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for k, m in report["summary"].items():
        L.append(
            f"| `{k}` | {m['requests']} | {m['success_pct']} | {m['404s']} | {m['429s']} | "
            f"{m['page1_200']} | {m['page2plus_total']} | {m['page2plus_success']} | "
            f"{m['max_page_reached']} | {m['median_ms']} |"
        )
    L += ["", "## Per-request detail", ""]
    for r in report["results"]:
        L.append(
            f"- `{r['variant']}` {r['endpoint']} {r['target']} p{r['page']}: "
            f"status={r['status_code']} {r['response_time_ms']}ms {r['http_version']} "
            f"parsed={r['parsed_ok']} tx={r['txid_source']}({r['txid_len']}) rate={r['rate_remaining']}"
            + (f" err={r['error']}" if r.get("error") else "")
        )
    pm.write_text("\n".join(L) + "\n")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Dynamic x-client-transaction-id hypothesis probe")
    ap.add_argument("--accounts", nargs="+",
                    default=["elonmusk", "Reuters", "realDonaldTrump", "TankerTrackers",
                             "KobeissiLetter", "EIAgov"])
    ap.add_argument("--pages", type=int, default=8)
    ap.add_argument("--endpoints", nargs="+",
                    default=["UserTweets", "UserTweetsAndReplies", "SearchTimeline"])
    ap.add_argument("--queries", nargs="+", default=["OpenAI", "Bitcoin", "Iran"])
    ap.add_argument("--inter-page", type=float, default=0.6)
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

    txgen = TxGenerator()
    tx_ok = txgen.seed(config.get("api_cookies", {}))

    # resolve target user ids (one UserByScreenName per account)
    user_ids = {a: P.resolve_user_id(cfg_path, a) for a in args.accounts}
    LOG.info("resolved: %s", {k: v for k, v in user_ids.items()})

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
        for label, uid, uname, rq in targets:
            for variant in ("A", "B"):
                if variant == "B" and not tx_ok:
                    LOG.warning("skipping B (tx-gen unavailable) for %s %s", endpoint, label)
                    continue
                LOG.info("=== %s : %s %s ===", variant, endpoint, label)
                results.extend(chain(manager, txgen, variant, config, endpoint, qid,
                                     label, uid, uname, rq, args.pages, args.inter_page))

    report = {
        "run_started": started,
        "run_finished": datetime.now().isoformat(),
        "config_path": str(cfg_path),
        "tx_seed": txgen.meta if tx_ok else {"error": txgen.seed_error},
        "params": vars(args),
        "resolved_user_ids": user_ids,
        "query_ids": QID,
        "summary": aggregate(results),
        "results": results,
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pj = REPORTS / f"dynamic_txid_{stamp}.json"
    pm = REPORTS / f"dynamic_txid_{stamp}.md"
    write_report(pj, pm, report)
    print(f"\nJSON: {pj}\nMD:   {pm}")
    print("\n=== SUMMARY ===")
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
