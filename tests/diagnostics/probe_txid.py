#!/usr/bin/env python3
"""Phase 2 decision probe: isolate WHY UserTweetsAndReplies gets 404'd.

Round 1 (tx-id variants) proved the x-client-transaction-id "badge" is NOT the
gate on its own: fake / omitted / random all 404'd identically while auth was
accepted (rate-limit still counted down). So this round isolates the two live
suspects with a small matrix -- reading network STATUS CODES only, no HTML/tag
scraping, so it can't rot when X changes its markup:

  endpoints x header-sets:
    UserTweets            (the door that WORKS in the runner -> control)
    UserTweetsAndReplies  (the stubborn door)
  x
    minimal   -> the headers the current code sends
    browser   -> minimal + sec-ch-ua*, accept*, origin, sec-fetch* (what a real
                 browser also sends)

Reads cookies/bearer from config.json, never prints them. Saves a sniffer-style
run under probe_runs/<utc-stamp>/ (results.md + results.jsonl).

How to read it (verdict is auto-printed):
  - UserTweets/minimal = 200 but UTAR/minimal = 404  -> endpoint-specific, same
    session+badge -> the badge isn't it; keep going.
  - UTAR/browser = 200                               -> missing browser HEADERS
    were the cause. Cheap fix: add them in twitter_http_client. No badge-maker.
  - UTAR/browser = 404 while UserTweets = 200        -> the endpoint needs a REAL
    signed badge. Phase 3 must build X's derived x-client-transaction-id.
  - UserTweets/minimal = 404 too                     -> cold-probe/session issue
    (cookies stale, or a warmup/UserByScreenName call is needed first).

    python tests/diagnostics/probe_txid.py
    python tests/diagnostics/probe_txid.py --user-id 44196397
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

import requests

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_DIAG_DIR = Path(__file__).resolve().parent
REPO_ROOT = _DIAG_DIR.parents[1]
CONFIG_PATH = REPO_ROOT / "src" / "shared" / "config" / "config.json"
PROBE_RUNS_DIR = _DIAG_DIR / "probe_runs"
DEFAULT_USER_ID = "22703645"  # public account that returned 200 in the capture

# endpoint -> (config query-id key, config payload key, referer path template)
ENDPOINTS = {
    "UserTweets": ("user_tweets_query_id", "UserTweets", "https://x.com/i/user/{uid}"),
    "UserTweetsAndReplies": (
        "user_tweets_and_replies_query_id",
        "UserTweetsAndReplies",
        "https://x.com/i/user/{uid}/with_replies",
    ),
    "SearchTimeline": (
        "search_timeline_query_id",
        "SearchTimeline",
        "https://x.com/search?q={query}&src=typed_query",
    ),
}


# --------------------------------------------------------------------------- #
# Request building (mirrors the working pagination-engine encoding exactly)
# --------------------------------------------------------------------------- #

def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _fake_id_like_current_code() -> str:
    # ponytail: mirror twitter_http_client._generate_transaction_id byte-for-byte.
    return base64.b64encode(os.urandom(72)).decode()[:94]


def _real_tx_id(cfg: dict, endpoint: str, idx: int = 0) -> str:
    pool_by_ep = cfg.get("real_transaction_ids_by_endpoint", {})
    pool = pool_by_ep.get(endpoint, cfg.get("real_transaction_ids", []))
    if pool:
        return pool[idx % len(pool)]
    return _fake_id_like_current_code()


def _build_url(cfg: dict, endpoint: str, user_id: str = "", query: str = "twitter") -> str:
    qid_key, payload_key, _ = ENDPOINTS[endpoint]
    qid = cfg["api_config"][qid_key]
    payload = cfg["graphql_endpoint_payloads"][payload_key]
    variables = dict(payload["variables"]["initial"])
    if endpoint == "SearchTimeline":
        variables["rawQuery"] = query
    elif user_id:
        variables["userId"] = user_id
    params = {"variables": _compact(variables), "features": _compact(payload["features"])}
    if isinstance(payload.get("fieldToggles"), dict):
        params["fieldToggles"] = _compact(payload["fieldToggles"])
    return f"https://x.com/i/api/graphql/{qid}/{endpoint}?{urlencode(params, quote_via=quote)}"


def _minimal_headers(cfg: dict, endpoint: str, user_id: str, txid: str) -> dict:
    referer_template = ENDPOINTS[endpoint][2]
    if endpoint == "SearchTimeline":
        referer = referer_template.format(query="twitter")
    else:
        referer = referer_template.format(uid=user_id)
    return {
        "authorization": f"Bearer {cfg['api_auth']['bearer_token']}",
        "x-csrf-token": cfg["api_cookies"].get("ct0", ""),  # must equal ct0 cookie
        "x-client-transaction-id": txid,
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "en",
        "content-type": "application/json",
        "referer": referer,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }


def _browser_headers(cfg: dict, endpoint: str, user_id: str, txid: str) -> dict:
    # minimal + the client-hint / fetch-metadata headers a real Chrome also sends.
    headers = _minimal_headers(cfg, endpoint, user_id, txid)
    headers.update({
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://x.com",
        "sec-ch-ua": '"Not)A;Brand";v="99", "Google Chrome";v="120", "Chromium";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    return headers


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #

def _probe_once(session, url: str, headers: dict, endpoint: str, header_set: str) -> dict:
    record = {
        "endpoint": endpoint,
        "header_set": header_set,
        "status": None,
        "ok": False,
        "rate_remaining": None,
        "top_level_keys": None,
        "body_snippet": None,
        "error": None,
    }
    try:
        resp = session.get(url, headers=headers, timeout=20)
    except requests.RequestException as exc:
        record["error"] = f"{exc.__class__.__name__}: {exc}"
        return record
    record["status"] = resp.status_code
    record["ok"] = resp.status_code == 200
    record["rate_remaining"] = resp.headers.get("x-rate-limit-remaining")
    try:
        body = resp.json()
        record["top_level_keys"] = sorted(body.keys()) if isinstance(body, dict) else None
        if not record["ok"]:
            record["body_snippet"] = json.dumps(body)[:300]
    except ValueError:
        record["body_snippet"] = resp.text[:300]
    return record


def _verdict(recs: dict) -> str:
    """recs keyed by (endpoint, header_set) -> record."""
    ut_min = recs[("UserTweets", "minimal")]
    utar_min = recs[("UserTweetsAndReplies", "minimal")]
    utar_br = recs[("UserTweetsAndReplies", "browser")]
    st_min = recs.get(("SearchTimeline", "minimal"), {"ok": False, "status": None})
    st_br = recs.get(("SearchTimeline", "browser"), {"ok": False, "status": None})

    if any(r["status"] is None for r in (ut_min, utar_min, utar_br)):
        return (
            "No HTTP verdict: at least one request failed before X returned a status "
            "(network reset/DNS/transport). Re-run from a network that can reach x.com."
        )
    if not ut_min["ok"]:
        return (
            "UserTweets ALSO 404'd from the cold probe -> this is a session/warmup "
            "issue, not endpoint-specific. Likely stale cookies or a required "
            "UserByScreenName/warmup call before profile timelines. Refresh cookies "
            "(auto_refresh.py --interactive) and/or warm up first, then re-probe."
        )
    if utar_br["ok"] and not utar_min["ok"]:
        return (
            "FIX FOUND (cheap): UserTweetsAndReplies returns 200 once the real "
            "browser headers (sec-ch-ua*/accept*/origin/sec-fetch*) are added. "
            "Phase 3 = add those headers in twitter_http_client. No badge-maker needed."
        )
    if ut_min["ok"] and not utar_min["ok"] and not utar_br["ok"]:
        return (
            "Endpoint-specific: same session+badge, UserTweets=200 but "
            "UserTweetsAndReplies=404 even with full browser headers. The remaining "
            "browser-only difference is the REAL signed x-client-transaction-id. "
            "Phase 3 must implement X's derived badge (variant D)."
        )
    if utar_min["ok"]:
        if st_min["ok"]:
            return "All endpoints return 200 with real endpoint-specific tx-ids — fix confirmed!"
        return "UserTweetsAndReplies now returns 200 with minimal headers — transient earlier 404; re-run to confirm."
    return "Inconclusive — see the table; paste results.md back for analysis."


def _write_run(out_dir: Path, user_id: str, records: list) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "results.jsonl").open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    lines = [
        f"# 404 Isolation Probe — {out_dir.name}",
        "",
        f"userId: `{user_id}`",
        "",
        "| Endpoint | Headers | tx-id len | HTTP | ok | rate-remaining | response |",
        "|---|---|---:|---|---|---|---|",
    ]
    for r in records:
        resp_note = (
            r["error"]
            or (",".join(r["top_level_keys"]) if r["top_level_keys"] else "")
            or (r["body_snippet"] or "")
        )
        lines.append(
            f"| {r['endpoint']} | {r['header_set']} | {r.get('txid_len')} | {r['status']} | "
            f"{'YES' if r['ok'] else 'no'} | {r['rate_remaining']} | {resp_note[:80]} |"
        )
    recs = {(r["endpoint"], r["header_set"]): r for r in records}
    lines += ["", "## Verdict", "", _verdict(recs), ""]
    (out_dir / "results.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 2 404-isolation probe")
    ap.add_argument("--user-id", default=DEFAULT_USER_ID)
    args = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    session = requests.Session()
    session.cookies.update({k: str(v) for k, v in cfg["api_cookies"].items() if v})
    real_txid_ut = _real_tx_id(cfg, "UserTweets", 0)
    real_txid_utar = _real_tx_id(cfg, "UserTweetsAndReplies", 0)
    real_txid_st = _real_tx_id(cfg, "SearchTimeline", 0)
    fake_txid = _fake_id_like_current_code()

    plan = [
        ("UserTweets", "minimal", _minimal_headers, real_txid_ut),
        ("UserTweetsAndReplies", "minimal", _minimal_headers, real_txid_utar),
        ("UserTweetsAndReplies", "browser", _browser_headers, real_txid_utar),
        ("SearchTimeline", "minimal", _minimal_headers, real_txid_st),
        ("SearchTimeline", "browser", _browser_headers, real_txid_st),
        ("UserTweetsAndReplies", "minimal_fake_txid", _minimal_headers, fake_txid),
    ]
    print(f"probe userId={args.user_id}\n")
    print(f"real tx-id (UserTweets):            {real_txid_ut[:30]}...  (endpoint-specific)")
    print(f"real tx-id (UserTweetsAndReplies): {real_txid_utar[:30]}...  (endpoint-specific)")
    print(f"real tx-id (SearchTimeline):       {real_txid_st[:30]}...  (endpoint-specific)")
    print(f"fake tx-id:  {fake_txid[:30]}...  (random)\n")
    records = []
    for endpoint, header_set, builder, txid in plan:
        query = "twitter" if endpoint == "SearchTimeline" else ""
        url = _build_url(cfg, endpoint, args.user_id, query)
        headers = builder(cfg, endpoint, args.user_id, txid)
        rec = _probe_once(session, url, headers, endpoint, header_set)
        rec["txid_len"] = len(txid)
        rec["txid_source"] = "real" if txid in [real_txid_ut, real_txid_utar, real_txid_st] else "fake"
        records.append(rec)
        print(f"[{endpoint}/{header_set}] HTTP {rec['status']}  "
              f"txid={rec['txid_source']}  rate-remaining={rec['rate_remaining']}"
              + (f"  ERR {rec['error']}" if rec["error"] else ""))

    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    out_dir = PROBE_RUNS_DIR / stamp
    _write_run(out_dir, args.user_id, records)

    recs = {(r["endpoint"], r["header_set"]): r for r in records}
    print("\n" + _verdict(recs))
    print(f"\nSaved: {out_dir}/results.md  (paste this back to me)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
