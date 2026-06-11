#!/usr/bin/env python3
"""
Twitter API Configuration Setup

This script helps you configure the Twitter scraper with your browser cookies
and API parameters.

WHAT YOU NEED:
1. Browser cookies from x.com (while logged in)
2. Bearer token (from Network tab)
3. Query IDs (from Network tab - optional, has defaults)

HOW TO GET THEM:
See CONFIG_GUIDE.md for detailed step-by-step instructions.

QUICK START:
1. Log in to x.com in your browser
2. Open DevTools (F12) → Application → Cookies
3. Copy all cookie values
4. Run this script and paste them when prompted
"""

import json
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
DEFAULT_TRANSACTION_ID = "Z5LW13y5+ps/Uciw/RJPTIXvJyhzpjV0wZBBgOeWohjwEMG2A0pgaN8s11s5Zq8R02R7y2Iv4XAluvl04DPn8bDWVCapZA"

ENDPOINT_KEY_MAP = {
    "UserByScreenName": "user_by_screen_name_query_id",
    "UserTweets": "user_tweets_query_id",
    "UserTweetsAndReplies": "user_tweets_and_replies_query_id",
    "TweetDetail": "tweet_detail_query_id",
    "SearchTimeline": "search_timeline_query_id",
}


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config" / "config.json"


def _extract_query_id_from_url(url: str):
    """Extract (endpoint, query_id) from a GraphQL request URL."""
    try:
        parsed = urlparse(url.strip())
        path = parsed.path or ""
        # Expected: /i/api/graphql/{QUERY_ID}/{ENDPOINT}
        parts = [p for p in path.split("/") if p]
        if "graphql" not in parts:
            return None, None
        graph_idx = parts.index("graphql")
        if len(parts) <= graph_idx + 2:
            return None, None

        query_id = parts[graph_idx + 1]
        endpoint = parts[graph_idx + 2]

        if endpoint in ENDPOINT_KEY_MAP and query_id:
            return endpoint, query_id
    except Exception:
        return None, None
    return None, None

def extract_query_ids_from_har(har_file_path):
    """Extract query IDs from HAR (HTTP Archive) file exported from browser."""
    import json
    
    try:
        with open(har_file_path, 'r') as f:
            har_data = json.load(f)
        
        query_ids = {}
        
        # Parse HAR entries
        for entry in har_data.get('log', {}).get('entries', []):
            url = entry.get('request', {}).get('url', '')
            endpoint, query_id = _extract_query_id_from_url(url)
            if endpoint and query_id:
                query_ids[ENDPOINT_KEY_MAP[endpoint]] = query_id
        
        return query_ids
    except Exception as e:
        print(f"✗ Error parsing HAR file: {e}")
        return {}

def extract_query_ids_from_text(text):
    """Extract query IDs from pasted network URLs."""
    import re
    
    query_ids = {}

    # Broad URL matcher so pasted DevTools dumps are supported.
    candidate_urls = re.findall(r'https?://[^\s"\'<>]+', text)
    for url in candidate_urls:
        endpoint, query_id = _extract_query_id_from_url(url)
        if endpoint and query_id:
            query_ids[ENDPOINT_KEY_MAP[endpoint]] = query_id
    
    return query_ids

def setup_query_ids_auto():
    """Interactive setup for automatic query ID extraction."""
    print("\n" + "=" * 70)
    print("Automatic Query ID Extraction")
    print("=" * 70)
    print("\nChoose method:")
    print("  1. Paste network URLs from browser DevTools")
    print("  2. Import HAR file (File → Save all as HAR)")
    print("  3. Manual entry")
    print()
    
    choice = input("Choice (1/2/3): ").strip()
    
    if choice == '1':
        print("\nPaste network URLs (one or more lines):")
        print("Example: https://x.com/i/api/graphql/6eh3huj6fJnA3Naupj4w0Q/UserTweetsAndReplies?...")
        print("Example: https://x.com/i/api/graphql/QUERY_ID/TweetDetail?...")
        print("(Press Ctrl+D or Ctrl+Z when done)")
        print()
        
        lines = []
        try:
            while True:
                line = input()
                lines.append(line)
        except EOFError:
            pass
        
        text = '\n'.join(lines)
        query_ids = extract_query_ids_from_text(text)
        
        if query_ids:
            print(f"\n✓ Extracted {len(query_ids)} query IDs:")
            for key, value in query_ids.items():
                endpoint = key.replace('_query_id', '').replace('_', ' ').title()
                print(f"  - {endpoint}: {value}")
            return query_ids
        else:
            print("\n✗ No query IDs found in pasted text")
            return None
    
    elif choice == '2':
        har_path = input("\nEnter path to HAR file: ").strip()
        query_ids = extract_query_ids_from_har(har_path)
        
        if query_ids:
            print(f"\n✓ Extracted {len(query_ids)} query IDs from HAR file")
            for key, value in query_ids.items():
                endpoint = key.replace('_query_id', '').replace('_', ' ').title()
                print(f"  - {endpoint}: {value}")
            return query_ids
        else:
            print("\n✗ No query IDs found in HAR file")
            return None
    
    else:
        return None




def setup_cookies():
    """Interactive setup for API cookies and configuration."""
    print("=" * 70)
    print("Twitter API Configuration Setup")
    print("=" * 70)
    print("\nThis script will help you configure:")
    print("  1. API Cookies (required)")
    print("  2. Bearer Token (required)")
    print("  3. Query IDs (optional - has defaults)")
    print("\n" + "=" * 70)
    
    config_file = CONFIG_FILE
    
    # Check if config exists
    if config_file.exists():
        print("\n✓ Found existing config file")
        with open(config_file, 'r') as f:
            config = json.load(f)
        print(f"  - Cookies: {len(config.get('api_cookies', {}))} cookies configured")
        print(f"  - Bearer token: {'✓' if config.get('api_auth', {}).get('bearer_token') else '✗'}")
        print(f"  - Extra API headers: {len(config.get('api_headers', {}))} configured")
        print(f"  - Query IDs: {'✓' if config.get('api_config') else '✗ (using defaults)'}")
        
        print("\nDo you want to:")
        print("  1. Update cookies only (most common)")
        print("  2. Update everything")
        print("  3. Exit")
        choice = input("\nChoice (1-3): ").strip()
        
        if choice == "3":
            print("\n✓ No changes made")
            return
        elif choice == "1":
            update_cookies_only(config, config_file)
            return
    else:
        config = {}
        print("\n⚠️  No config file found. Creating new configuration...")
    
    # Full setup
    print("\n" + "=" * 70)
    print("STEP 1: Browser Cookies")
    print("=" * 70)
    print("\nHow to get cookies:")
    print("  1. Open x.com in browser (logged in)")
    print("  2. Press F12 → Application tab → Cookies → x.com")
    print("  3. Copy the entire cookie string")
    print("\nExample format:")
    print("  auth_token=abc123; ct0=def456; twid=u%3D789...")
    print()
    
    cookies_str = input("Paste your cookies here: ").strip()
    
    if not cookies_str:
        print("\n✗ No cookies provided. Using existing or defaults.")
        cookies_dict = config.get('api_cookies', {})
    else:
        # Parse cookies
        cookies_dict = {}
        for cookie in cookies_str.split('; '):
            if '=' in cookie:
                key, value = cookie.split('=', 1)
                cookies_dict[key] = value
        print(f"\n✓ Parsed {len(cookies_dict)} cookies")
    
    # Bearer token
    print("\n" + "=" * 70)
    print("STEP 2: Bearer Token")
    print("=" * 70)
    print("\nHow to get bearer token:")
    print("  1. Open x.com → F12 → Network tab")
    print("  2. Filter by 'graphql'")
    print("  3. Click any request → Headers → authorization")
    print("  4. Copy the token after 'Bearer '")
    print("\nDefault (usually works):")
    print(f"  {DEFAULT_BEARER_TOKEN}")
    print()
    
    current_bearer = config.get('api_auth', {}).get('bearer_token', DEFAULT_BEARER_TOKEN)
    bearer_token = input("Bearer token (press Enter to keep current/default): ").strip()
    if not bearer_token:
        bearer_token = current_bearer
        print("✓ Using existing/default bearer token")

    print("\n" + "=" * 70)
    print("STEP 3: Required API Headers")
    print("=" * 70)
    print("\nSome X GraphQL requests require x-client-transaction-id.")
    print("Find it in DevTools → Network → graphql request → Headers.")
    print("Press Enter to keep the existing/default value.")
    print()

    existing_headers = config.get('api_headers', {})
    current_transaction_id = existing_headers.get('x-client-transaction-id', DEFAULT_TRANSACTION_ID)
    transaction_id = input("x-client-transaction-id: ").strip() or current_transaction_id
    api_headers = dict(existing_headers)
    api_headers['x-client-transaction-id'] = transaction_id
    
    # Query IDs
    print("\n" + "=" * 70)
    print("STEP 4: Query IDs (Optional)")
    print("=" * 70)
    print("\nQuery IDs change occasionally. Update only if you get 404 errors.")
    print("Before collecting URLs/HAR, browse these pages in X:")
    print("  1. https://x.com/explore")
    print("  2. https://x.com/<username>")
    print("  3. https://x.com/<username>/with_replies")
    print("  4. https://x.com/<username>/status/<tweet_id>")
    print("\nThis helps capture fresh IDs for:")
    print("  - UserByScreenName")
    print("  - UserTweets")
    print("  - UserTweetsAndReplies")
    print("  - TweetDetail")
    print("  - SearchTimeline")
    print("See CONFIG_GUIDE.md for how to find them in Network tab.")
    print()
    
    update_query_ids = input("Update query IDs? (y/N): ").strip().lower()
    existing_api_config = config.get('api_config', {})
    
    if update_query_ids == 'y':
        # Try automatic extraction first
        try:
            auto_ids = setup_query_ids_auto()
        except Exception as e:
            print(f"\n⚠️  Automatic extraction failed: {e}")
            print("Falling back to manual entry...")
            auto_ids = None
        
        if auto_ids:
            # Use automatically extracted IDs
            api_config = dict(existing_api_config)
            api_config.update(auto_ids)
        else:
            # Fallback to manual entry
            print("\nEnter query IDs manually (press Enter to keep current/default):")
            
            current_user_by_screen_name = existing_api_config.get("user_by_screen_name_query_id", "sLVLhk0bGj3MVFEKTdax1w")
            current_user_tweets = existing_api_config.get("user_tweets_query_id", "naBcZ4al-iTCFBYGOAMzBQ")
            current_user_tweets_replies = existing_api_config.get("user_tweets_and_replies_query_id", "6eh3huj6fJnA3Naupj4w0Q")
            current_tweet_detail = existing_api_config.get("tweet_detail_query_id", "")
            current_search_timeline = existing_api_config.get("search_timeline_query_id", "")
            user_by_screen_name = input(f"  UserByScreenName [{current_user_by_screen_name}]: ").strip()
            user_tweets = input(f"  UserTweets [{current_user_tweets}]: ").strip()
            user_tweets_replies = input(f"  UserTweetsAndReplies [{current_user_tweets_replies}]: ").strip()
            tweet_detail = input(f"  TweetDetail [{current_tweet_detail}]: ").strip()
            search_timeline = input(f"  SearchTimeline [{current_search_timeline}]: ").strip()
            
            api_config = dict(existing_api_config)
            api_config.update({
                "user_by_screen_name_query_id": user_by_screen_name or current_user_by_screen_name,
                "user_tweets_query_id": user_tweets or current_user_tweets,
                "user_tweets_and_replies_query_id": user_tweets_replies or current_user_tweets_replies,
                "tweet_detail_query_id": tweet_detail or current_tweet_detail,
                "search_timeline_query_id": search_timeline or current_search_timeline,
            })
    else:
        # Use existing or defaults
        api_config = existing_api_config or {
            "user_by_screen_name_query_id": "sLVLhk0bGj3MVFEKTdax1w",
            "user_tweets_query_id": "naBcZ4al-iTCFBYGOAMzBQ",
            "user_tweets_and_replies_query_id": "6eh3huj6fJnA3Naupj4w0Q",
            "tweet_detail_query_id": "",
            "search_timeline_query_id": "",
        }
        print("✓ Using existing/default query IDs")

    api_config.setdefault("search_timeline_query_id", "")
    
    # Build final config
    config['api_cookies'] = cookies_dict
    config['api_auth'] = {'bearer_token': bearer_token}
    config['api_headers'] = api_headers
    config['api_config'] = api_config
    config.setdefault('anti_bot_simulation', {
        "enabled": True,
        "browse_warmup_enabled": True,
        "warmup_pages": 2,
        "delays_seconds": {
            "before_request_min": 0.2,
            "before_request_max": 1.2,
            "between_requests_min": 0.5,
            "between_requests_max": 2.5,
            "between_pages_min": 2,
            "between_pages_max": 6,
            "replies_retry_min": 1,
            "replies_retry_max": 3,
            "between_accounts_min": 3,
            "between_accounts_max": 8,
            "between_cycles_min": 0,
            "between_cycles_max": 60
        }
    })
    
    # Save
    config_file.parent.mkdir(exist_ok=True)
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)
    
    print("\n" + "=" * 70)
    print("✅ Configuration saved!")
    print("=" * 70)
    print(f"\nSaved to: {config_file}")
    print("\nConfiguration summary:")
    print(f"  - Cookies: {len(cookies_dict)} configured")
    print(f"  - Bearer token: {bearer_token[:50]}...")
    print(f"  - Extra API headers: {len(api_headers)} configured")
    print(f"  - Query IDs: {len(api_config)} configured")
    
    print("\n" + "=" * 70)
    print("Next steps:")
    print("=" * 70)
    print("  1. Run: python3 fetch_historical_tweets_hybrid.py")
    print("  2. If you get errors, see CONFIG_GUIDE.md")
    print("  3. Update cookies when they expire (every 30-90 days)")
    print()

def update_cookies_only(config, config_file):
    """Quick update for cookies only."""
    print("\n" + "=" * 70)
    print("Update Cookies Only")
    print("=" * 70)
    print("\nPaste your new cookies (from browser DevTools):")
    print("Format: auth_token=abc; ct0=def; twid=ghi...")
    print()
    
    cookies_str = input("Cookies: ").strip()
    
    if not cookies_str:
        print("\n✗ No cookies provided. No changes made.")
        return
    
    # Parse cookies
    cookies_dict = {}
    for cookie in cookies_str.split('; '):
        if '=' in cookie:
            key, value = cookie.split('=', 1)
            cookies_dict[key] = value
    
    config['api_cookies'] = cookies_dict
    config.setdefault('api_headers', {})
    config['api_headers'].setdefault('x-client-transaction-id', DEFAULT_TRANSACTION_ID)
    
    # Save
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)
    
    print(f"\n✓ Updated {len(cookies_dict)} cookies")
    print(f"✓ Saved to: {config_file}")
    print("\n✅ Ready to run fetch_historical_tweets_hybrid.py")

if __name__ == "__main__":
    try:
        setup_cookies()
    except KeyboardInterrupt:
        print("\n\n✗ Setup cancelled")
    except Exception as e:
        print(f"\n✗ Error: {e}")
        print("\nFor help, see CONFIG_GUIDE.md")
