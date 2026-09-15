from __future__ import annotations
"""Playwright browser bootstrap and target-only GraphQL capture for Twitter/X."""


import json
import time
from hashlib import sha256
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse


# X pins document.body.scrollHeight to the viewport height and grows
# document.documentElement instead, so the obvious
# `scrollTo(0, document.body.scrollHeight)` moves roughly one screen and never
# reaches the sentinel row that triggers the next page. Measured on a live
# search: body-scroll produced 1 SearchTimeline call, documentElement-scroll
# produced 6 over the same number of steps. Take the max so this stays correct
# if X ever moves the growth back onto body.
SCROLL_TO_BOTTOM = (
    "window.scrollTo(0, Math.max("
    "document.body.scrollHeight, document.documentElement.scrollHeight))"
)


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
    target_page_meta: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    route_retry_count: int = 0
    stop_reason: Optional[str] = None
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

    # Docker gives a container 64MB of /dev/shm by default, and Chromium puts its
    # renderer's shared memory there. A long search timeline exhausts that and the
    # renderer dies mid-scroll ("Target crashed"), which the capture loop could
    # only report as a stall -- this was the real cause of shallow deep-search
    # runs, not scroll mechanics. --disable-dev-shm-usage moves that allocation to
    # /tmp, so capture survives regardless of the host's shm sizing.
    # Deep search scrolling builds a very long DOM, and the worker shares a small
    # Docker VM with Postgres/Redis/web. These keep one headless tab inside that
    # budget so a long capture cannot OOM the machine.
    LAUNCH_ARGS = (
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-features=Translate,MediaRouter",
        "--mute-audio",
        "--blink-settings=imagesEnabled=false",  # timeline JSON is captured, not pixels
        # Do NOT cap V8's old space here. Measured on a live deep search: an
        # explicit --max-old-space-size=384 starved the renderer and captured 0
        # pages before crashing, while the same run uncapped captured 5.
    )

    @classmethod
    def _launch_chromium(cls, pw: Any, *, headless: bool):
        """Use the Playwright-managed Chromium installed in the runtime image."""
        return pw.chromium.launch(headless=headless, args=list(cls.LAUNCH_ARGS))

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
        stop_when: Optional[Callable[[Dict[str, Any]], bool]] = None,
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
        max_pages = max(1, int(max_pages))
        try:
            with sync_playwright() as pw:
                browser = self._launch_chromium(pw, headless=self.headless)
                # A taller-than-default viewport keeps more of X's virtualized
                # timeline mounted per scroll. Kept at 1200: at 2000 the renderer
                # exceeds the worker's memory ceiling and Chromium hard-crashes
                # ("Target crashed") partway through a deep search capture.
                context = browser.new_context(viewport={"width": 1280, "height": 1200})
                if pw_cookies:
                    context.add_cookies(pw_cookies)
                page = context.new_page()
                seen_page_keys: set[str] = set()
                stop_capture = False
                target_response_count = 0

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
                    nonlocal stop_capture, target_response_count
                    parsed = self._endpoint(response.url)
                    if not parsed or response.status != 200:
                        return
                    endpoint, _ = parsed
                    if endpoint != capture_endpoint:
                        return
                    target_response_count += 1
                    try:
                        payload = response.json()
                    except Exception:
                        return
                    variables = self._params(response.url).get("variables", {})
                    input_cursor = variables.get("cursor") if isinstance(variables, dict) else None
                    page_key = str(input_cursor or sha256(
                        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest())
                    if page_key in seen_page_keys:
                        return
                    pages = result.target_pages.setdefault(endpoint, [])
                    if len(pages) < max_pages:
                        seen_page_keys.add(page_key)
                        pages.append(payload)
                        result.target_page_meta.setdefault(endpoint, []).append({
                            "status": int(response.status),
                            "has_input_cursor": bool(input_cursor),
                            "captured_at": time.time(),
                        })
                        if stop_when and stop_when(payload):
                            stop_capture = True
                            result.stop_reason = "predicate"

                page.on("request", on_request)
                page.on("response", on_response)

                def visit(target: str) -> bool:
                    try:
                        page.goto(target, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    except Exception:
                        return False
                    # SearchTimeline GraphQL often lands after first paint; give it time.
                    page.wait_for_timeout(4500)
                    return True

                def wait_for_activity(baseline: int, budget_ms: int) -> bool:
                    """Poll for a new captured page instead of sleeping a flat interval.

                    Fixed 3.5s sleeps both wasted time when X answered in 400ms and
                    gave up too early when it was slow.
                    """
                    waited = 0
                    while waited < budget_ms:
                        page.wait_for_timeout(250)
                        waited += 250
                        if target_response_count > baseline or stop_capture:
                            return True
                    return False

                def capture_scroll() -> None:
                    nonlocal stop_capture
                    stagnant = 0
                    pages = result.target_pages.setdefault(capture_endpoint or "", [])
                    while capture_endpoint and len(pages) < max_pages and not stop_capture and stagnant < 6:
                        before_activity = target_response_count
                        page.evaluate(SCROLL_TO_BOTTOM)
                        if not wait_for_activity(before_activity, 6000):
                            # Virtualized timelines occasionally remain pinned at the
                            # bottom. A bounded upward nudge re-arms IntersectionObserver.
                            page.evaluate("window.scrollBy(0, -Math.max(1200, window.innerHeight))")
                            page.wait_for_timeout(600)
                            page.evaluate(SCROLL_TO_BOTTOM)
                            wait_for_activity(before_activity, 6000)
                        stagnant = 0 if target_response_count > before_activity else stagnant + 1
                    if not result.stop_reason:
                        result.stop_reason = "max_pages" if len(pages) >= max_pages else "stalled"

                if search_url:
                    visit(search_url)
                    capture_scroll()
                    if (
                        capture_endpoint
                        and result.stop_reason == "stalled"
                        and result.target_pages.get(capture_endpoint)
                        and len(result.target_pages[capture_endpoint]) < max_pages
                    ):
                        # Retry the direct search route once. Response activity—not
                        # uniqueness—keeps the duplicate replay from tripping stall
                        # detection before it reaches the prior cursor depth.
                        result.route_retry_count = 1
                        result.stop_reason = None
                        if visit(search_url):
                            capture_scroll()
                    if capture_endpoint and not result.target_pages.get(capture_endpoint):
                        for target in ("https://x.com/home", "https://x.com/explore", search_url):
                            visit(target)
                        capture_scroll()
                else:
                    routes = ["https://x.com/home"]
                    if username:
                        routes.append(f"https://x.com/{username}")
                        if capture_endpoint != "UserTweets":
                            routes.append(f"https://x.com/{username}/with_replies")
                    for target in routes:
                        visit(target)
                    capture_scroll()
                result.cookies = {item["name"]: item["value"] for item in context.cookies()}
                browser.close()
                if capture_endpoint and not result.target_pages.get(capture_endpoint):
                    result.ok = False
                    result.stop_reason = "no_target_response"
                    result.error = "no_target_response"
        except Exception as exc:
            # Chromium can hard-crash late in a long capture (huge timeline DOM).
            # Pages already captured are complete, validated GraphQL payloads, so
            # keep them instead of throwing the whole run away -- discarding them
            # is what turned a 5-page deep search into a 1-page "stalled" result.
            captured = result.target_pages.get(capture_endpoint or "", [])
            result.ok = bool(captured)
            result.stop_reason = "crashed_with_partial_capture" if captured else "crashed"
            result.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        return result
