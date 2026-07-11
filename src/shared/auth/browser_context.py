from __future__ import annotations
"""Playwright browser bootstrap and target-only GraphQL capture."""


import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse


@dataclass
class BrowserBootstrapResult:
    ok: bool
    route: str
    cookies: Dict[str, str] = field(default_factory=dict)
    query_ids: Dict[str, str] = field(default_factory=dict)
    request_headers: Dict[str, Dict[str, str]] = field(default_factory=dict)
    request_contracts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    support_request_count: int = 0
    target_pages: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    error: Optional[str] = None


class BrowserBootstrap:
    """Execute X routes in a real JS browser while retaining target data only."""

    ENDPOINTS = {"UserByScreenName", "UserTweets", "UserTweetsAndReplies", "SearchTimeline"}

    def __init__(self, config: Dict[str, Any], *, headless: bool = True, timeout_ms: int = 60000):
        self.config = config
        self.headless = headless
        self.timeout_ms = timeout_ms

    @staticmethod
    def available() -> bool:
        try:
            import playwright.sync_api  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def _endpoint(url: str) -> Optional[tuple[str, str]]:
        parts = [part for part in urlparse(url).path.split("/") if part]
        if "graphql" not in parts:
            return None
        index = parts.index("graphql")
        if len(parts) <= index + 2:
            return None
        return parts[index + 2], parts[index + 1]

    @staticmethod
    def _params(url: str) -> Dict[str, Any]:
        raw = parse_qs(urlparse(url).query)
        out: Dict[str, Any] = {}
        for key in ("variables", "features", "fieldToggles"):
            if raw.get(key):
                try:
                    out[key] = json.loads(unquote(raw[key][0]))
                except Exception:
                    pass
        return out

    def run(
        self,
        *,
        username: Optional[str] = None,
        search_url: Optional[str] = None,
        capture_endpoint: Optional[str] = None,
        max_pages: int = 2,
    ) -> BrowserBootstrapResult:
        route = search_url or (f"https://x.com/{username}" if username else "https://x.com/home")
        if not self.available():
            return BrowserBootstrapResult(False, route, error="playwright_not_installed")
        from playwright.sync_api import sync_playwright

        result = BrowserBootstrapResult(True, route)
        cookies = self.config.get("api_cookies", {}) or {}
        pw_cookies = [
            {"name": str(k), "value": str(v), "domain": ".x.com", "path": "/"}
            for k, v in cookies.items() if v
        ]
        routes = ["https://x.com/home"]
        if search_url:
            routes.extend(["https://x.com/explore", search_url])
        elif username:
            routes.append(f"https://x.com/{username}")
            if capture_endpoint != "UserTweets":
                routes.append(f"https://x.com/{username}/with_replies")
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self.headless)
                context = browser.new_context()
                if pw_cookies:
                    context.add_cookies(pw_cookies)
                page = context.new_page()

                def on_request(request) -> None:
                    parsed = self._endpoint(request.url)
                    if not parsed:
                        return
                    result.support_request_count += 1
                    endpoint, query_id = parsed
                    if endpoint not in self.ENDPOINTS:
                        return
                    result.query_ids[endpoint] = query_id
                    result.request_headers[endpoint] = {str(k).lower(): str(v) for k, v in request.headers.items()}
                    result.request_contracts[endpoint] = self._params(request.url)

                def on_response(response) -> None:
                    parsed = self._endpoint(response.url)
                    if not parsed or response.status != 200:
                        return
                    endpoint, _ = parsed
                    if endpoint != capture_endpoint:
                        return
                    try:
                        payload = response.json()
                    except Exception:
                        return
                    pages = result.target_pages.setdefault(endpoint, [])
                    if len(pages) < max_pages:
                        pages.append(payload)

                page.on("request", on_request)
                page.on("response", on_response)
                for target in routes:
                    page.goto(target, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    page.wait_for_timeout(2500)
                    if capture_endpoint and target == routes[-1]:
                        for _ in range(max(0, max_pages - 1)):
                            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            page.wait_for_timeout(2500)
                result.cookies = {item["name"]: item["value"] for item in context.cookies()}
                browser.close()
        except Exception as exc:
            result.ok = False
            result.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        return result
