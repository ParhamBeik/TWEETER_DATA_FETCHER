#!/usr/bin/env python3
"""
Automated session refresh via Playwright.

Fully automated: loads cookies from config, visits profile -> replies -> search 
with scrolling, intercepts GraphQL tx-ids + query-ids, saves to config.

This is the primary authentication maintenance tool. It:
- Automatically uses cookies from config.json
- Visits multiple pages to trigger all endpoint types
- Captures fresh x-client-transaction-id values
- Captures fresh query IDs
- Updates ct0 cookie if changed
- Works in headless mode by default
- Can also run in headed mode for manual login when cookies are expired

Triggered by twitter_http_client when all tx-ids for an endpoint are stale.
Can also be run manually from CLI.
"""

import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright, Request
except ImportError:
    sync_playwright = None

logger = logging.getLogger(__name__)

# Hardcoded username/query to fire all endpoint types
PROFILE_USERNAME = "elonmusk"
SEARCH_QUERY = "twitter"
SCROLL_COUNT = 3
SCROLL_PAUSE_SECONDS = 2
TIMEOUT_PER_PAGE = 15

# Maps GraphQL endpoint names to config.json key names
ENDPOINT_KEY_MAP: Dict[str, str] = {
    "UserByScreenName": "user_by_screen_name_query_id",
    "UserTweets": "user_tweets_query_id",
    "UserTweetsAndReplies": "user_tweets_and_replies_query_id",
    "TweetDetail": "tweet_detail_query_id",
    "SearchTimeline": "search_timeline_query_id",
}

# Default query IDs (fallback if not captured)
DEFAULT_QUERY_IDS: Dict[str, str] = {
    "user_by_screen_name_query_id": "sLVLhk0bGj3MVFEKTdax1w",
    "user_tweets_query_id": "pQHADmT91zIY83UbK0x4Lw",
    "user_tweets_and_replies_query_id": "xdqXQQg4vOBF9Np6VtUsdw",
    "tweet_detail_query_id": "",
    "search_timeline_query_id": "099UqLkXma7fhT81Jv4n9g",
}


def _extract_query_id_from_url(url: str) -> Optional[tuple[str, str]]:
    """Extract (endpoint, query_id) from a GraphQL request URL."""
    try:
        parsed = urlparse(url.strip())
        parts = [p for p in (parsed.path or "").split("/") if p]
        if "graphql" not in parts:
            return None
        graph_idx = parts.index("graphql")
        if len(parts) <= graph_idx + 2:
            return None
        query_id = parts[graph_idx + 1]
        endpoint = parts[graph_idx + 2]
        if endpoint in ENDPOINT_KEY_MAP and query_id:
            return (endpoint, query_id)
    except Exception:
        pass
    return None


def _load_config(config_path: Path) -> Dict[str, Any]:
    """Load config from JSON file."""
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_config(config_path: Path, cfg: Dict[str, Any]) -> None:
    """Persist the updated config to disk with atomic write and backup."""
    if config_path.exists():
        backup_path = config_path.with_suffix(".json.bak")
        shutil.copy2(config_path, backup_path)
        logger.info("Config backup saved to %s", backup_path)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
    try:
        with open(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        Path(tmp_path).replace(config_path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    logger.info("Config saved to %s", config_path)


def _cookiestring_to_playwright(cookies_dict: Dict[str, str]) -> List[Dict[str, Any]]:
    """Convert a flat {name: value} dict into Playwright cookie format."""
    return [
        {
            "name": name,
            "value": str(value),
            "domain": ".x.com",
            "path": "/",
        }
        for name, value in cookies_dict.items()
    ]


def auto_refresh_session(
    config_path: Path,
    endpoints: Optional[List[str]] = None,
    timeout_per_page: int = TIMEOUT_PER_PAGE,
    headless: bool = True,
    interactive_login: bool = False,
) -> bool:
    """
    Launch Playwright browser, inject cookies, visit profile/replies/search,
    collect endpoint-specific tx-ids + query-ids, save to config.

    Args:
        config_path: Path to shared/config/config.json
        endpoints: Unused (always visits all pages to collect full set)
        timeout_per_page: Seconds per page navigation
        headless: Run browser in headless mode (default True for automation)
        interactive_login: If True, opens headed browser for manual login

    Returns:
        True if parameters were captured and config updated, False otherwise
    """
    if sync_playwright is None:
        print("\u274c Playwright not installed. Run: pip3 install playwright && playwright install chromium")
        return False

    config_path = Path(config_path)
    
    try:
        config = _load_config(config_path)
    except FileNotFoundError as e:
        print(f"\u274c {e}")
        return False

    cookies = config.get("api_cookies", {})
    
    # Check if we have valid cookies
    has_auth_token = bool(cookies.get("auth_token"))
    has_ct0 = bool(cookies.get("ct0"))
    
    if not has_auth_token and not interactive_login:
        print("\u274c No auth_token in config. Run with --interactive for manual login.")
        return False
    
    if not has_ct0 and not interactive_login:
        print("\u26a0 Warning: No ct0 cookie in config. Session may be incomplete.")

    intercepted_tx_ids: Dict[str, List[str]] = {}
    intercepted_query_ids: Dict[str, str] = {}
    extracted_cookies: Dict[str, str] = {}
    extracted_tx_id: Optional[str] = None

    def _on_request(request: Request) -> None:
        url = request.url
        headers = request.headers

        # Extract query IDs from GraphQL URLs
        if "/graphql/" in url:
            result = _extract_query_id_from_url(url)
            if result:
                endpoint, query_id = result
                key = ENDPOINT_KEY_MAP.get(endpoint)
                if key:
                    intercepted_query_ids[endpoint] = query_id

            # Capture x-client-transaction-id
            tx_val = headers.get("x-client-transaction-id")
            if tx_val and len(tx_val) == 94:
                result = _extract_query_id_from_url(url)
                if result:
                    endpoint, _ = result
                    if endpoint in ENDPOINT_KEY_MAP:
                        if endpoint not in intercepted_tx_ids:
                            intercepted_tx_ids[endpoint] = []
                        if tx_val not in intercepted_tx_ids[endpoint]:
                            intercepted_tx_ids[endpoint].append(tx_val)
                
                # Also track the first tx-id we see for headers
                if not extracted_tx_id:
                    extracted_tx_id = tx_val

    print(f"[*] Launching {'headless' if headless else 'headed'} browser for auto-refresh...")
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                user_agent=config.get("api_headers", {}).get("user-agent", 
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            )
            
            # Inject existing cookies if available
            if cookies:
                pw_cookies = _cookiestring_to_playwright(cookies)
                context.add_cookies(pw_cookies)
            
            page = context.new_page()
            page.on("request", _on_request)
            
            # For interactive login, open home page and wait for user
            if interactive_login:
                print("\n" + "=" * 60)
                print("  Browser window will open shortly.")
                print("  Please log in to your X (Twitter) account.")
                print("=" * 60)
                print()
                print("After logging in:")
                print("  1. Navigate to x.com/home (or any page)")
                print("  2. Wait for the page to fully load")
                print("  3. Do NOT close the browser yet")
                print()
                print("Waiting up to 120 seconds for you to log in...")
                print("(Press Ctrl+C at any time to cancel)\n")
                
                page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
                
                # Poll for home page to confirm session is active
                login_timeout = 120
                start = time.time()
                while time.time() - start < login_timeout:
                    try:
                        url = page.url
                        if "home" in url.lower() or "x.com" in url.lower():
                            time.sleep(3)  # let initial requests fire
                            break
                    except Exception:
                        pass
                    time.sleep(2)
                
                print("Session active. Waiting for data extraction...")
                time.sleep(10)
            else:
                # Automated mode: visit specific pages
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
            
            # Read back updated cookies from browser
            for cookie in context.cookies():
                extracted_cookies[cookie["name"]] = cookie["value"]
            
            page.close()
            context.close()
            browser.close()
        
    except KeyboardInterrupt:
        logger.info("Auto refresh cancelled by user.")
        print("\n\u2717 Cancelled.\n")
        return False
    except Exception as exc:
        logger.error("Auto refresh failed: %s", exc)
        print(f"\n\u274c Auto refresh failed: {exc}\n")
        return False

    # Check if we captured anything
    if not intercepted_tx_ids and not extracted_cookies:
        print("\u274c No parameters captured. Check cookies or network.")
        return False

    # Apply extracted data to config
    changes_made = False
    
    # Update cookies if we have fresh ones
    if extracted_cookies:
        critical_cookies = {"auth_token", "ct0"}
        has_critical = any(c in extracted_cookies for c in critical_cookies)
        
        if has_critical:
            # Update all cookies with fresh values
            config["api_cookies"] = extracted_cookies
            changes_made = True
            print(f"[+] Updated {len(extracted_cookies)} cookies")
    
    # Update transaction IDs
    if intercepted_tx_ids:
        # Build endpoint-specific tx-id dict
        tx_by_endpoint = {ep: txs[:10] for ep, txs in intercepted_tx_ids.items()}
        config["real_transaction_ids_by_endpoint"] = tx_by_endpoint
        
        # Also keep flat list for backward compatibility
        all_tx = [tx for txs in intercepted_tx_ids.values() for tx in txs]
        config["real_transaction_ids"] = all_tx[:20]
        
        # Update the default tx-id in headers if we captured one
        if extracted_tx_id:
            config.setdefault("api_headers", {})
            config["api_headers"]["x-client-transaction-id"] = extracted_tx_id
        
        changes_made = True
        print(f"[+] Captured {sum(len(txs) for txs in intercepted_tx_ids.values())} tx-ids across {len(intercepted_tx_ids)} endpoints")
    
    # Update query IDs
    if intercepted_query_ids:
        api_config = config.setdefault("api_config", {})
        for endpoint, query_id in intercepted_query_ids.items():
            config_key = ENDPOINT_KEY_MAP[endpoint]
            api_config[config_key] = query_id
        changes_made = True
        print(f"[+] Updated {len(intercepted_query_ids)} query-ids")
    
    if not changes_made:
        print("\u2717 No changes to save.")
        return False
    
    # Save config
    _save_config(config_path, config)
    
    print(f"\n[+] Config updated: {config_path}")
    if intercepted_tx_ids:
        for ep, txs in intercepted_tx_ids.items():
            print(f"    - {ep}: {len(txs)} tx-ids")
    if intercepted_query_ids:
        for ep, qid in intercepted_query_ids.items():
            print(f"    - {ep}: query_id = {qid}")
    
    return True


def run_interactive_cli() -> None:
    """Interactive CLI for auto-refresh with mode selection."""
    print("\n" + "=" * 60)
    print("  Twitter Session Auto-Refresh")
    print("=" * 60)
    print()
    print("Choose a refresh mode:")
    print()
    print("  1. Quick Refresh (Automated)")
    print("     - Uses existing cookies from config.json")
    print("     - Runs in headless mode")
    print("     - Works only if your session is still active")
    print("     - Faster, no manual login needed")
    print()
    print("  2. Full Login (Interactive)")
    print("     - Opens a visible browser window")
    print("     - Log in manually to X/Twitter")
    print("     - Extracts all fresh parameters:")
    print("       cookies, x-client-transaction-id, query IDs")
    print("     - Use when quick refresh fails")
    print()

    try:
        choice = input("Choose (1/2, default 1): ").strip() or "1"
    except EOFError:
        choice = "1"

    config_path = Path(__file__).resolve().parents[1] / "config" / "config.json"
    
    try:
        if choice == "1":
            success = auto_refresh_session(config_path, headless=True, interactive_login=False)
        elif choice == "2":
            success = auto_refresh_session(config_path, headless=False, interactive_login=True)
        else:
            print(f"\nInvalid choice: {choice}. Use 1 or 2.")
            sys.exit(1)
    except FileNotFoundError as e:
        print(f"\nError: {e}")
        print("Run auto_refresh.py with --interactive flag first to create a config file.")
        sys.exit(1)

    if success:
        print("\n\u2705 You can now run your scripts:")
        print("  python3 historical_scripts/historical_pipeline.py")
        print("  python3 live_scripts/live_pipeline.py")
        print("  python3 search_scripts/search_pipeline.py --once")
    else:
        print(
            "\n\u274c Refresh failed. Ensure you have an active internet"
            " connection and valid X/Twitter credentials.\n"
        )
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Automated Twitter session refresh tool"
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Open headed browser for manual login"
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        dest="headed",
        help="Run browser in headed mode (same as --interactive)"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config.json (default: shared/config/config.json)"
    )
    
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    
    if args.interactive or args.headed:
        # Interactive mode
        config_path = Path(args.config) if args.config else (
            Path(__file__).resolve().parents[1] / "config" / "config.json"
        )
        success = auto_refresh_session(
            config_path,
            headless=False,
            interactive_login=True
        )
        sys.exit(0 if success else 1)
    elif len(sys.argv) > 1:
        # Non-interactive mode with custom config
        config_path = Path(args.config) if args.config else (
            Path(__file__).resolve().parents[1] / "config" / "config.json"
        )
        success = auto_refresh_session(config_path, headless=True, interactive_login=False)
        sys.exit(0 if success else 1)
    else:
        # Interactive CLI mode (no args)
        run_interactive_cli()
