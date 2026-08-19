#!/usr/bin/env python3
"""Prove which scroll target drives X's SearchTimeline infinite scroll.

X pins document.body.scrollHeight to the viewport height while
document.documentElement.scrollHeight is the real content height, so
`window.scrollTo(0, document.body.scrollHeight)` barely moves and the
infinite-scroll sentinel is never reached. This probe scrolls both ways and
counts SearchTimeline GraphQL calls, so the capture fix is evidence-backed.

Run inside the SaaS worker image:
    python search_scroll_probe.py <config.json> [--mode body|doc] [--scrolls 12]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

sys.path.insert(0, "/app/fetcher")

from playwright.sync_api import sync_playwright  # noqa: E402

from tweeter_data_fetcher.x_api.browser import BrowserBootstrap  # noqa: E402

BODY_SCROLL = "window.scrollTo(0, document.body.scrollHeight)"
DOC_SCROLL = (
    "window.scrollTo(0, Math.max("
    "document.body.scrollHeight, document.documentElement.scrollHeight))"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--mode", choices=["body", "doc"], default="doc")
    parser.add_argument("--scrolls", type=int, default=12)
    parser.add_argument("--height", type=int, default=2000)
    parser.add_argument(
        "--raw-query",
        default="(Iran OR War OR Brent OR Gold OR Inflation OR Hormuz) lang:en since:2026-05-23",
    )
    args = parser.parse_args()

    cfg = json.load(open(args.config))
    cookies = [
        {"name": str(k), "value": str(v), "domain": ".x.com", "path": "/"}
        for k, v in (cfg.get("api_cookies") or {}).items()
        if v
    ]
    url = (
        "https://x.com/search?q="
        + urllib.parse.quote(args.raw_query)
        + "&src=typed_query&f=live"
    )
    script = DOC_SCROLL if args.mode == "doc" else BODY_SCROLL
    hits: list[int] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=list(BrowserBootstrap.LAUNCH_ARGS))
        ctx = browser.new_context(viewport={"width": 1280, "height": args.height})
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        page.on(
            "response",
            lambda r: hits.append(r.status) if "SearchTimeline" in r.url else None,
        )
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        print(f"mode={args.mode} viewport_h={args.height}")
        print(
            "  body.scrollHeight=%s docEl.scrollHeight=%s"
            % (
                page.evaluate("document.body.scrollHeight"),
                page.evaluate("document.documentElement.scrollHeight"),
            )
        )
        for i in range(args.scrolls):
            page.evaluate(script)
            page.wait_for_timeout(2600)
            print(
                "  s%-2d y=%-6s docH=%-6s arts=%-3s calls=%s"
                % (
                    i + 1,
                    page.evaluate("window.scrollY"),
                    page.evaluate("document.documentElement.scrollHeight"),
                    page.eval_on_selector_all("article", "e=>e.length"),
                    len(hits),
                ),
                flush=True,
            )
        print(f"RESULT mode={args.mode} total_SearchTimeline_calls={len(hits)}")
        browser.close()


if __name__ == "__main__":
    main()
