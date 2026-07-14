#!/usr/bin/env python3
"""Probe whether route/request sequence unlocks replies/search before tx-id work.

Run:
    python tools/diagnostics/probe_sequence.py
    python tools/diagnostics/probe_sequence.py --profiles 44196397:elonmusk

Flags:
    --profiles <id:user ...>  profile pairs to probe
    --search-query <query>    SearchTimeline query
    --search-product <name>   search product (default Top)
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

_DIAG_DIR = Path(__file__).resolve().parent
REPO_ROOT = _DIAG_DIR.parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
from tweeter_data_fetcher.configuration import resolve_config_path

CONFIG_PATH = resolve_config_path(project_root=REPO_ROOT)
PROBE_RUNS_DIR = _DIAG_DIR / "probe_runs"
PROFILES = {
    "22703645": "tuckercarlson",
    "44196397": "elonmusk",
}
USER_BY_SCREEN_NAME_FEATURES = {
    "hidden_profile_subscriptions_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}
USER_BY_SCREEN_NAME_FIELD_TOGGLES = {
    "withPayments": False,
    "withAuxiliaryUserLabels": True,
}
QID_KEYS = {
    "UserByScreenName": "user_by_screen_name_query_id",
    "UserTweets": "user_tweets_query_id",
    "UserTweetsAndReplies": "user_tweets_and_replies_query_id",
    "SearchTimeline": "search_timeline_query_id",
}


def compact(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"))


def fake_txid() -> str:
    return base64.b64encode(os.urandom(72)).decode()[:94]


def url(cfg: dict, endpoint: str, variables: dict, features: dict, field_toggles: dict | None = None) -> str:
    params = {"variables": compact(variables), "features": compact(features)}
    if field_toggles is not None:
        params["fieldToggles"] = compact(field_toggles)
    qid = cfg["api_config"][QID_KEYS[endpoint]]
    return f"https://x.com/i/api/graphql/{qid}/{endpoint}?{urlencode(params, quote_via=quote)}"


def timeline_url(cfg: dict, endpoint: str, user_id: str) -> str:
    payload = cfg["graphql_endpoint_payloads"][endpoint]
    variables = dict(payload["variables"]["initial"], userId=user_id)
    return url(
        cfg,
        endpoint,
        variables,
        dict(payload["features"]),
        dict(payload["fieldToggles"]),
    )


def user_url(cfg: dict, username: str) -> str:
    variables = {"screen_name": username, "withGrokTranslatedBio": True}
    return url(
        cfg,
        "UserByScreenName",
        variables,
        USER_BY_SCREEN_NAME_FEATURES,
        USER_BY_SCREEN_NAME_FIELD_TOGGLES,
    )


def search_url(cfg: dict, raw_query: str, product: str) -> str:
    variables = {
        "rawQuery": raw_query,
        "count": 20,
        "querySource": "typed_query",
        "product": product,
        "withGrokTranslatedBio": True,
        "withQuickPromoteEligibilityTweetFields": False,
    }
    return url(cfg, "SearchTimeline", variables, dict(cfg["graphql_endpoint_payloads"]["SearchTimeline"]["features"]))


def headers(cfg: dict, referer: str, txid: str) -> dict:
    return {
        "authorization": f"Bearer {cfg['api_auth']['bearer_token']}",
        "x-csrf-token": cfg["api_cookies"].get("ct0", ""),
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
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://x.com",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def knock(session: requests.Session, label: str, endpoint: str, request_url: str, request_headers: dict) -> dict:
    record = {
        "label": label,
        "endpoint": endpoint,
        "referer": request_headers.get("referer"),
        "status": None,
        "ok": False,
        "rate_remaining": None,
        "response": "",
    }
    try:
        resp = session.get(request_url, headers=request_headers, timeout=20)
    except requests.RequestException as exc:
        record["response"] = f"{exc.__class__.__name__}: {exc}"
        return record
    record["status"] = resp.status_code
    record["ok"] = resp.status_code == 200
    record["rate_remaining"] = resp.headers.get("x-rate-limit-remaining")
    try:
        body = resp.json()
        record["response"] = ",".join(sorted(body)) if record["ok"] and isinstance(body, dict) else json.dumps(body)[:160]
    except ValueError:
        record["response"] = resp.text[:160]
    return record


def profile_steps(cfg: dict, user_id: str, username: str, sequence: str) -> list[tuple[str, str, str, str]]:
    home = f"https://x.com/{username}"
    replies = f"{home}/with_replies"
    steps = {
        "direct_replies": [("replies", "UserTweetsAndReplies", timeline_url(cfg, "UserTweetsAndReplies", user_id), replies)],
        "tweets_then_replies": [
            ("tweets", "UserTweets", timeline_url(cfg, "UserTweets", user_id), home),
            ("replies", "UserTweetsAndReplies", timeline_url(cfg, "UserTweetsAndReplies", user_id), replies),
        ],
        "screenname_then_replies": [
            ("screenname", "UserByScreenName", user_url(cfg, username), home),
            ("replies", "UserTweetsAndReplies", timeline_url(cfg, "UserTweetsAndReplies", user_id), replies),
        ],
        "screenname_tweets_replies": [
            ("screenname", "UserByScreenName", user_url(cfg, username), home),
            ("tweets", "UserTweets", timeline_url(cfg, "UserTweets", user_id), home),
            ("replies", "UserTweetsAndReplies", timeline_url(cfg, "UserTweetsAndReplies", user_id), replies),
        ],
    }
    return steps[sequence]


def write_results(out_dir: Path, records: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sequence_results.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )
    lines = [
        f"# Sequence Probe — {out_dir.name}",
        "",
        "| Case | Step | Endpoint | tx-id len | HTTP | ok | rate | referer | response |",
        "|---|---|---|---:|---:|---|---:|---|---|",
    ]
    for r in records:
        lines.append(
            f"| {r['case']} | {r['step']} | {r['endpoint']} | {r.get('txid_len')} | {r['status']} | "
            f"{'YES' if r['ok'] else 'no'} | {r['rate_remaining']} | `{r['referer']}` | {r['response'][:80]} |"
        )
    lines += ["", "## Quick read", ""]
    reply_hits = [r for r in records if r["endpoint"] == "UserTweetsAndReplies" and r["ok"]]
    search_hits = [r for r in records if r["endpoint"] == "SearchTimeline" and r["ok"]]
    if any(r["status"] is None for r in records):
        lines.append("No HTTP verdict: at least one request failed before X returned a status.")
    elif reply_hits:
        lines.append("Replies returned 200 in at least one sequence; use the shortest passing sequence.")
    else:
        lines.append("Replies stayed blocked in every fake-tx sequence; signed tx-id remains the next suspect.")
    if any(r["endpoint"] == "SearchTimeline" and r["status"] is None for r in records):
        lines.append("SearchTimeline also had no HTTP verdict.")
    elif search_hits:
        lines.append("SearchTimeline returned 200 with direct search referer.")
    else:
        lines.append("SearchTimeline did not return 200 in this direct search-referer probe.")
    (out_dir / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe route sequence/referer before signed tx-id work.")
    parser.add_argument("--profiles", nargs="*", default=[f"{uid}:{name}" for uid, name in PROFILES.items()])
    parser.add_argument("--search-query", default="WAR min_replies:100 min_faves:10000 min_retweets:100 since:2026-01-01")
    parser.add_argument("--search-product", default="Top")
    args = parser.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    records: list[dict] = []

    for profile in args.profiles:
        user_id, username = profile.split(":", 1)
        for sequence in ("direct_replies", "tweets_then_replies", "screenname_then_replies", "screenname_tweets_replies"):
            session = requests.Session()
            session.cookies.update({k: str(v) for k, v in cfg["api_cookies"].items() if v})
            txid = fake_txid()
            print(f"\n[{username}/{sequence}]")
            for step, endpoint, request_url, referer in profile_steps(cfg, user_id, username, sequence):
                rec = knock(session, f"{username}:{sequence}", endpoint, request_url, headers(cfg, referer, txid))
                rec.update({"case": f"{username}:{sequence}", "step": step, "txid_len": len(txid)})
                records.append(rec)
                print(f"  {step:10s} {endpoint:22s} HTTP {rec['status']} rate={rec['rate_remaining']}")

    session = requests.Session()
    session.cookies.update({k: str(v) for k, v in cfg["api_cookies"].items() if v})
    human_search_url = f"https://x.com/search?q={quote(args.search_query)}&f={args.search_product.lower()}&src=typed_query"
    txid = fake_txid()
    rec = knock(
        session,
        "search:direct",
        "SearchTimeline",
        search_url(cfg, args.search_query, args.search_product),
        headers(cfg, human_search_url, txid),
    )
    rec.update({"case": "search:direct", "step": "search", "txid_len": len(txid)})
    records.append(rec)
    print(f"\n[search/direct] SearchTimeline HTTP {rec['status']} rate={rec['rate_remaining']}")

    out_dir = PROBE_RUNS_DIR / dt.datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S_sequence")
    write_results(out_dir, records)
    print(f"\nSaved: {out_dir}/results.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
