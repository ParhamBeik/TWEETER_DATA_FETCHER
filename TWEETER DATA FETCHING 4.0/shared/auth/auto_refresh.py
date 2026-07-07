#!/usr/bin/env python3
"""
Automated session refresh via headless Playwright.

Fully automated (no manual steps): loads cookies from config, visits profile →
replies → search with scrolling, intercepts GraphQL tx-ids + query-ids, saves
to config. Triggered by twitter_http_client when all tx-ids for an endpoint
are stale.
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from playwright.sync_api import sync_playwright, Request
except ImportError:
    sync_playwright = None

# ponytail: hardcoded username/query, enough to fire all 3 endpoint types
PROFILE_USERNAME = "elonmusk"
SEARCH_QUERY = "twitter"
SCROLL_COUNT = 3
SCROLL_PAUSE_SECONDS = 2
TIMEOUT_PER_PAGE = 15

ENDPOINT_KEY_MAP = {
    "UserByScreenName": "user_by_screen_name_query_id",
    "UserTweets": "user_tweets_query_id",
    "UserTweetsAndReplies": "user_tweets_and_replies_query_id",
    "SearchTimeline": "search_timeline_query_id",
}


def auto_refresh_session(
    config_path: Path,
    endpoints: Optional[List[str]] = None,
    timeout_per_page: int = TIMEOUT_PER_PAGE,
) -> bool:
    """
    Launch headless Playwright, inject cookies, visit profile/replies/search,
    collect endpoint-specific tx-ids + query-ids, save to config.

    Args:
        config_path: Path to shared/config/config.json
        endpoints: Unused (always visits all 3 pages to collect full set)
        timeout_per_page: Seconds per page navigation

    Returns:
        True if tx-ids captured, False otherwise
    """
    if sync_playwright is None:
        print("❌ Playwright not installed. Run: pip3 install playwright && playwright install chromium")
        return False

    config_path = Path(config_path)
    if not config_path.exists():
        print(f"❌ Config not found: {config_path}")
        return False

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    cookies = config.get("api_cookies", {})
    if not cookies.get("auth_token"):
        print("❌ No auth_token in config. Run query_ids_updater.py first.")
        return False

    intercepted_tx_ids: Dict[str, List[str]] = {}
    intercepted_query_ids: Dict[str, str] = {}

    def _on_request(request: Request):
        url = request.url
        if "/graphql/" not in url:
            return
        
        # Extract endpoint name from URL: /graphql/{queryId}/{EndpointName}
        parts = url.split("/graphql/")
        if len(parts) < 2:
            return
        graphql_parts = parts[1].split("/")
        if len(graphql_parts) < 2:
            return
        
        query_id = graphql_parts[0]
        endpoint = graphql_parts[1].split("?")[0]
        
        # Collect query-id
        if endpoint in ENDPOINT_KEY_MAP:
            intercepted_query_ids[endpoint] = query_id
        
        # Collect tx-id
        headers = request.headers
        tx_val = headers.get("x-client-transaction-id")
        if tx_val and len(tx_val) == 94 and endpoint in ENDPOINT_KEY_MAP:
            if endpoint not in intercepted_tx_ids:
                intercepted_tx_ids[endpoint] = []
            if tx_val not in intercepted_tx_ids[endpoint]:
                intercepted_tx_ids[endpoint].append(tx_val)

    print("[*] Launching headless browser for auto-refresh...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=config.get("api_headers", {}).get("user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        )
        
        # Inject cookies
        for name, value in cookies.items():
            if not value:
                continue
            try:
                context.add_cookies([{
                    "name": str(name),
                    "value": str(value),
                    "domain": ".x.com",
                    "path": "/",
                }])
            except Exception:
                pass
        
        page = context.new_page()
        page.on("request", _on_request)
        
        # Visit 3 pages in sequence
        pages = [
            (f"https://x.com/{PROFILE_USERNAME}", "profile"),
            (f"https://x.com/{PROFILE_USERNAME}/with_replies", "replies"),
            (f"https://x.com/search?q={SEARCH_QUERY}&src=typed_query", "search"),
        ]
        
        for url, label in pages:
            try:
                print(f"[*] Visiting {label}: {url}")
                page.goto(url, timeout=timeout_per_page * 1000, wait_until="networkidle")
                
                # Scroll to trigger more requests
                for i in range(SCROLL_COUNT):
                    page.evaluate("window.scrollBy(0, window.innerHeight)")
                    time.sleep(SCROLL_PAUSE_SECONDS)
                
                print(f"    Collected {sum(len(txs) for txs in intercepted_tx_ids.values())} tx-ids so far")
            except Exception as e:
                print(f"    Warning: {e}")
        
        page.close()
        context.close()
        browser.close()
    
    if not intercepted_tx_ids:
        print("❌ No tx-ids captured. Check cookies or network.")
        return False
    
    # Save to config
    print(f"[+] Captured {sum(len(txs) for txs in intercepted_tx_ids.values())} tx-ids across {len(intercepted_tx_ids)} endpoints")
    
    tx_by_endpoint = {ep: txs[:10] for ep, txs in intercepted_tx_ids.items()}
    config["real_transaction_ids_by_endpoint"] = tx_by_endpoint
    
    all_tx = [tx for txs in intercepted_tx_ids.values() for tx in txs]
    config["real_transaction_ids"] = all_tx[:20]
    
    if intercepted_query_ids:
        api_config = config.setdefault("api_config", {})
        for endpoint, query_id in intercepted_query_ids.items():
            config_key = ENDPOINT_KEY_MAP[endpoint]
            api_config[config_key] = query_id
        print(f"[+] Updated {len(intercepted_query_ids)} query-ids")
    
    # Atomic write
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
    try:
        with open(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        Path(tmp_path).replace(config_path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    
    print(f"[+] Config updated: {config_path}")
    for ep, txs in intercepted_tx_ids.items():
        print(f"    - {ep}: {len(txs)} tx-ids")
    
    return True


if __name__ == "__main__":
    import sys
    config_path = Path(__file__).resolve().parents[1] / "config" / "config.json"
    success = auto_refresh_session(config_path)
    sys.exit(0 if success else 1)
