#!/usr/bin/env python3
"""
Enhanced X.com GraphQL interceptor – decodes features & fieldToggles.
Prints everything you need to replicate the exact API calls.
"""

import asyncio
import json
import re
from datetime import datetime
from urllib.parse import unquote, urlparse, parse_qs

from playwright.async_api import async_playwright

# ------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------
URLS_TO_VISIT = [
    "https://x.com/search?q=NEWS%20(Iran%20OR%20War%20OR%20Brent%20OR%20Gold%20OR%20Inflation%20OR%20Hormuz)%20min_replies%3A100%20min_faves%3A1000%20min_retweets%3A50%20lang%3Aen%20since%3A2026-05-10&f=top&src=typed_query"
]

# Your cookies (the same raw Cookie header you already use)
RAW_COOKIE_STRING = (
    "guest_id=v1%3A177711975217751018; "
    "__cuid=371bef68d36541d08b0f1992cf805185; "
    "g_state={\"i_l\":0,\"i_ll\":1777148016989,\"i_e\":{\"enable_itp_optimization\":0},\"i_et\":1777147970292}; "
    "kdt=lN34hDtC2uXY0wtApwoiRPvF4D4QrZZnl3qEquR0; "
    "auth_token=df51ac6bd02c2cc631982c57c7f175cb650e18a4; "
    "ct0=1bbc00f49ff258c217c809e5e1db70be507b05c51065fe5c280cd9f731c01cfc95d4c271995ab46486cfd175d958b0e59b6ae36b419cc2a603b52111dcd6468ee6d12cbe9a11180319729ddf912e198c; "
    "twid=u%3D1076002579962871808; "
    "d_prefs=MToxLGNvbnNlbnRfdmVyc2lvbjoyLHRleHRfdmVyc2lvbjoxMDAw; "
    "guest_id_ads=v1%3A177711975217751018; "
    "guest_id_marketing=v1%3A177711975217751018; "
    "personalization_id=\"v1_nRj8IpZT3VVqxpbeudDDBA==\"; "
    "lang=en"
)

# ------------------------------------------------------------------
# Cookie parser
# ------------------------------------------------------------------
def parse_cookie_string(raw: str):
    cookies = []
    for pair in raw.split(";"):
        if "=" not in pair:
            continue
        key, value = pair.strip().split("=", 1)
        cookies.append({
            "name": key,
            "value": value,
            "domain": ".x.com",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "None"
        })
    return cookies


# ------------------------------------------------------------------
# GraphQL response handler
# ------------------------------------------------------------------
async def on_graphql(response):
    url = response.url
    if "/graphql/" not in url:
        return

    match = re.search(r"/graphql/([^/]+)/([^?]+)", url)
    if not match:
        return
    operation_id, operation_name = match.group(1), match.group(2)

    # Parse query string and decode everything
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    # Decode variables (already handled in previous version)
    variables_raw = params.get("variables", [""])[0]
    try:
        variables = json.loads(unquote(variables_raw))
    except:
        variables = variables_raw

    # Decode features (the big one)
    features_raw = params.get("features", [""])[0]
    try:
        features = json.loads(unquote(features_raw))
    except:
        features = features_raw

    # Decode fieldToggles (sometimes present)
    fieldtoggles_raw = params.get("fieldToggles", [""])[0]
    try:
        fieldtoggles = json.loads(unquote(fieldtoggles_raw))
    except:
        fieldtoggles = fieldtoggles_raw if fieldtoggles_raw else None

    # Decode extraParams if any
    extraparams_raw = params.get("extraParams", [""])[0]
    try:
        extraparams = json.loads(unquote(extraparams_raw)) if extraparams_raw else None
    except:
        extraparams = extraparams_raw if extraparams_raw else None

    # Print a clean, structured summary
    print(f"\n{'='*60}")
    print(f"📊 {operation_name} (ID: {operation_id})")
    print(f"   Status: {response.status}")

    # Variables
    if isinstance(variables, dict):
        print("   Variables:")
        for k, v in variables.items():
            print(f"      {k}: {v}")
    else:
        print(f"   Variables: {variables}")

    # Features – pretty print the full JSON
    if features:
        print("   Features:")
        print(json.dumps(features, indent=6, sort_keys=True))

    # FieldToggles
    if fieldtoggles:
        print("   FieldToggles:")
        print(json.dumps(fieldtoggles, indent=6, sort_keys=True))

    # ExtraParams
    if extraparams:
        print("   ExtraParams:")
        print(json.dumps(extraparams, indent=6, sort_keys=True))

    # Optionally show a short response structure
    try:
        body = await response.json()
        print("   Response top-level keys: " + ", ".join(body.keys()))
    except:
        print("   Response: not valid JSON")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
async def main():
    print("🌐 Launching browser with your cookies...")
    cookies = parse_cookie_string(RAW_COOKIE_STRING)

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir="./x_browser_data",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800},
        )
        page = await browser.new_page()
        await browser.add_cookies(cookies)

        # Attach interceptor
        page.on("response", on_graphql)

        for url in URLS_TO_VISIT:
            print(f"\n🚀 Opening {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # Let the page settle and load more content
                await asyncio.sleep(8)
            except Exception as e:
                print(f"⚠️  Could not load: {e}")

        print("\n✅ Initial load complete. Browser stays open so you can manually scroll or click.")
        print("   Press Ctrl+C to close and exit.")
        try:
            await asyncio.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())