#!/usr/bin/env python3
"""
Build and send authenticated Twitter/X GraphQL requests.

Code map:
- Session setup loads cookies, headers, and query IDs from config.
- Request context chooses the route/referer for each endpoint.
- Rate-limit helpers read response headers and decide when to sleep.
- perform_get is the main low-level request method used by runners.
"""

import json
import logging
import time
import base64
import os
import random
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

from tweeter_data_fetcher.configuration import resolve_config_path
from tweeter_data_fetcher.paths import PROJECT_ROOT
from tweeter_data_fetcher.x_api.request_state import RequestStateStore

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util import Retry
except ImportError:
    print("ERROR: Missing requests. Run: pip3 install requests")
    raise

logger = logging.getLogger(__name__)


# Small request state objects ------------------------------------------------


class EndpointHealth:
    """Track endpoint health status"""
    HEALTHY = "healthy"
    STALE_QUERY_ID = "stale_query_id"
    CONTEXT_REJECTED = "context_rejected"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    UNKNOWN_ERROR = "unknown_error"


@dataclass(frozen=True)
class RequestContext:
    """
    Route-aware browser context for one API request.

    X derives several request headers from the current route/runtime state. Keep
    these values request-scoped so retries can vary context without mutating the
    long-lived authenticated session.
    """
    name: str
    endpoint: str
    referer: str
    active_user: str = "yes"
    warmup_routes: Tuple[str, ...] = ()


# HTTP session and request helpers ------------------------------------------


class APIManager:
    """Centralized API communication manager"""
    
    # Request costs per endpoint (for budget accounting)
    REQUEST_COSTS = {
        "UserByScreenName": 1,
        "UserTweets": 2,
        "UserTweetsAndReplies": 3,
        "TweetDetail": 2,
        "SearchTimeline": 2,
    }
    
    # Default rate limits (overridden by config)
    DEFAULT_LIMITS = {
        "UserByScreenName": {"limit": 150, "window_seconds": 900},
        "UserTweets": {"limit": 50, "window_seconds": 900},
        "UserTweetsAndReplies": {"limit": 500, "window_seconds": 900},
        "TweetDetail": {"limit": 150, "window_seconds": 900},
        "SearchTimeline": {"limit": 50, "window_seconds": 900},
    }
    PARAM_FAILURE_LIMIT = 3
    QUERY_ID_CONFIG_KEYS = {
        "UserByScreenName": "user_by_screen_name_query_id",
        "UserTweets": "user_tweets_query_id",
        "UserTweetsAndReplies": "user_tweets_and_replies_query_id",
        "TweetDetail": "tweet_detail_query_id",
        "SearchTimeline": "search_timeline_query_id",
    }

    @staticmethod
    def _resolve_config_path(config_path: Optional[str]) -> Path:
        return resolve_config_path(config_path)

    # مسیر پیش‌فرض کانفیگ اصلاح شد
    def __init__(
        self,
        config_path: Optional[str] = None,
        state_dir: Optional[Path] = None,
        console: Optional[Any] = None,
        recorder: Optional[Any] = None,
    ):
        self.config_path = self._resolve_config_path(config_path)
        self.config = self._load_config()
        self.simulation_config = self.config.get("anti_bot_simulation", {})
        self.default_timeout = int(
            self.config.get("api_config", {}).get("default_timeout_seconds", 20)
        )
        
        # Observability injection
        self.console = console
        self.recorder = recorder
        
        # State directory for persistent tracking
        self.state_dir = state_dir or (PROJECT_ROOT / "data" / "historical_live" / "state")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.shared_state_dir = self.state_dir
        self.shared_state_dir.mkdir(parents=True, exist_ok=True)
        self.request_state = RequestStateStore(
            self.state_dir,
            failure_limit=self.PARAM_FAILURE_LIMIT,
        )
        
        # Real browser transaction IDs pool (endpoint-specific)
        self.real_tx_ids = self.config.get("real_transaction_ids_by_endpoint", {})
        # Fallback to flat list for backward compatibility
        if not self.real_tx_ids:
            self.real_tx_ids = {"_default": self.config.get("real_transaction_ids", [])}
        self.tx_id_indices = {}  # Track index per endpoint
        
        # Per-tx-id state tracking (healthy/stale)
        self.tx_id_state: Dict[str, Dict[str, Any]] = self._load_tx_id_state()
        self.query_id_state: Dict[str, Dict[str, Any]] = self._load_query_id_state()
        self.query_id_indices: Dict[str, int] = {}
        
        # 404 counter for auto-refresh trigger
        self.consecutive_404s: Dict[str, int] = {}
        self.auto_refresh_attempts: Dict[Tuple[str, str], int] = {}
        
        # Session setup
        self.session = requests.Session()
        self._setup_session()
        
        # Rate limit tracking per endpoint
        self.rate_limits: Dict[str, Dict] = self._load_rate_limits()
        
        # Endpoint health tracking
        self.endpoint_health: Dict[str, str] = self._load_endpoint_health()
        self.last_status_by_endpoint: Dict[str, Optional[int]] = {}
        
        # Query IDs
        self.query_ids = self._load_query_ids()
        self.query_id_pools = self._load_query_id_pools()

        # Request accounting
        self.request_count = 0
        self.session_start = time.time()

    def _info(self, message: str) -> None:
        console = getattr(self, "console", None)
        if console:
            console.info(message)
        else:
            logger.info(message)

    def _warning(self, message: str) -> None:
        console = getattr(self, "console", None)
        if console:
            console.warning(message)
        else:
            logger.warning(message)

    def _error(self, message: str) -> None:
        console = getattr(self, "console", None)
        if console:
            console.error(message)
        else:
            logger.error(message)

    def _request_state_store(self) -> RequestStateStore:
        store = getattr(self, "request_state", None)
        if store is None:
            store = RequestStateStore(
                self.state_dir,
                failure_limit=self.PARAM_FAILURE_LIMIT,
            )
            self.request_state = store
        return store

    def _load_tx_id_state(self) -> Dict[str, Dict[str, Any]]:
        """Load per-tx-id state (healthy/stale) from disk."""
        return self._request_state_store().load("tx_id_state.json", {})

    def _load_query_id_state(self) -> Dict[str, Dict[str, Any]]:
        return self._request_state_store().load("query_id_state.json", {})

    def _save_query_id_state(self):
        self._request_state_store().save("query_id_state.json", self.query_id_state)

    def _param_failures(self, state_value: Any) -> int:
        if isinstance(state_value, dict):
            return int(state_value.get("failures", 0) or 0)
        return self.PARAM_FAILURE_LIMIT if state_value == "stale" else 0

    def _param_ruled_out(self, state_value: Any) -> bool:
        if isinstance(state_value, dict):
            return state_value.get("status") == "stale" or self._param_failures(state_value) >= self.PARAM_FAILURE_LIMIT
        return state_value == "stale"
    
    def _save_tx_id_state(self):
        """Persist per-tx-id state to disk."""
        self._request_state_store().save("tx_id_state.json", self.tx_id_state)
    
    def _mark_tx_id(self, endpoint: str, tx_id: str, state: str):
        """Track tx-id health; 404 rule-out happens on the third consecutive failure."""
        result = self._request_state_store().mark_parameter(
            "tx_id_state.json",
            endpoint,
            tx_id,
            healthy=state == "healthy",
        )
        self.tx_id_state.setdefault(endpoint, {})[tx_id] = result

    def _mark_query_id(self, endpoint: str, query_id: str, state: str):
        result = self._request_state_store().mark_parameter(
            "query_id_state.json",
            endpoint,
            query_id,
            healthy=state == "healthy",
        )
        self.query_id_state.setdefault(endpoint, {})[query_id] = result
    
    def _all_tx_ids_stale(self, endpoint: str) -> bool:
        """Check if every tx-id in the pool for this endpoint is marked stale."""
        pool = self.real_tx_ids.get(endpoint, self.real_tx_ids.get("_default", []))
        if not pool:
            return False
        
        state_map = self.tx_id_state.get(endpoint, {})
        for tx_id in pool:
            if not self._param_ruled_out(state_map.get(tx_id)):
                return False
        
        return True

    def _all_query_ids_stale(self, endpoint: str) -> bool:
        pool = self.query_id_pools.get(endpoint, [])
        if not pool:
            return False
        state_map = self.query_id_state.get(endpoint, {})
        return all(self._param_ruled_out(state_map.get(query_id)) for query_id in pool)
        
    def _load_config(self) -> dict:
        """Load configuration from JSON"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config not found: {self.config_path}")
        with open(self.config_path) as f:
            return json.load(f)
    
    def _setup_session(self):
        """Configure session with auth and headers"""
        # Cookies
        cookies = self.config.get("api_cookies", {})
        for key, value in cookies.items():
            self.session.cookies.set(key, value, domain=".x.com")
        
        # Bearer token
        bearer = self.config.get("api_auth", {}).get("bearer_token", "")
        
        # CSRF token from cookies
        csrf_token = cookies.get("ct0", "")
        
        configured_headers = self.config.get("api_headers", {})
        
        tx_id = configured_headers.get("x-client-transaction-id") or self._generate_transaction_id()

        # Stable browser/session headers copied from the GraphQL sniffer logs.
        # Route-specific requests override only referer/active-user.
        self.session.headers.update({
            "authorization": f"Bearer {bearer}",
            "x-csrf-token": csrf_token,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "x-client-transaction-id": tx_id,
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "referer": "https://x.com/",
            "content-type": "application/json",
            "sec-ch-ua": '"Not/A)Brand";v="99", "Chromium";v="148"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        })
        if configured_headers:
            self.session.headers.update({k: str(v) for k, v in configured_headers.items() if v})
        self._configure_http_adapter()

    def _configure_http_adapter(self):
        """Attach resilient retry strategy for unstable network conditions."""
        retry_strategy = Retry(
            total=5,
            connect=5,
            read=5,
            status=5,
            backoff_factor=2,
            # Keep 429 visible to fetcher code so it can persist rate-limit
            # headers, sleep until reset, and retry the exact same cursor/page.
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=30, pool_maxsize=30)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _get(self, url: str, **kwargs) -> requests.Response:
        """Session GET with global timeout default."""
        kwargs.setdefault("timeout", self.default_timeout)
        return self.session.get(url, **kwargs)
    
    def _generate_transaction_id(self, endpoint: str = "_default") -> str:
        """
        Get next real browser transaction ID from the endpoint-specific pool.
        Falls back to random generation if pool is empty.
        """
        pool = self.real_tx_ids.get(endpoint, self.real_tx_ids.get("_default", []))
        if pool:
            # Skip stale tx-ids, pick next healthy/unmarked one
            state_map = self.tx_id_state.get(endpoint, {})
            # Rotate through real captured tx-ids
            if endpoint not in self.tx_id_indices:
                self.tx_id_indices[endpoint] = 0
            
            for _ in range(len(pool)):
                tx_id = pool[self.tx_id_indices[endpoint] % len(pool)]
                self.tx_id_indices[endpoint] += 1
                if not self._param_ruled_out(state_map.get(tx_id)):
                    return tx_id
            
            # All stale, return last one anyway (auto-refresh will trigger)
            return tx_id
        else:
            # Fallback to random (should not happen with updated config)
            return base64.b64encode(os.urandom(72)).decode()[:94]

    def _default_referer(self, endpoint: str, username: Optional[str] = None) -> str:
        if username:
            if endpoint == "UserTweetsAndReplies":
                return f"https://x.com/{username}/with_replies"
            if endpoint in ["UserTweets", "UserByScreenName"]:
                return f"https://x.com/{username}"
        if endpoint == "SearchTimeline":
            return "https://x.com/search?q=twitter&src=typed_query"
        return "https://x.com/"

    def get_context_variants(self, endpoint: str, username: Optional[str] = None) -> List[RequestContext]:
        """Return the small context set from the original reliable fetcher."""
        profile_url = f"https://x.com/{username}" if username else "https://x.com/"
        replies_url = f"{profile_url}/with_replies" if username else "https://x.com/"

        if endpoint == "UserTweetsAndReplies":
            return [
                RequestContext("replies_tab_active", endpoint, replies_url, "yes", (replies_url,)),
                RequestContext("replies_tab_passive", endpoint, replies_url, "no", (replies_url,)),
                RequestContext("home_fallback", endpoint, "https://x.com/", "yes", ()),
            ]

        if endpoint == "UserTweets":
            return [RequestContext("user_profile", endpoint, profile_url, "yes", (profile_url,))]

        if endpoint == "UserByScreenName":
            return [
                RequestContext("profile_lookup", endpoint, profile_url, "yes", (profile_url,)),
                RequestContext("home_lookup", endpoint, "https://x.com/", "yes", ("https://x.com/home",)),
            ]

        if endpoint == "SearchTimeline":
            return [
                RequestContext(
                    "search_timeline_main",
                    endpoint,
                    "https://x.com/search?q=twitter&src=typed_query",
                    "yes",
                    ("https://x.com/search?q=twitter&src=typed_query",),
                ),
                RequestContext(
                    "search_timeline_explore",
                    endpoint,
                    "https://x.com/explore",
                    "yes",
                    ("https://x.com/explore",),
                ),
            ]

        return [RequestContext("default", endpoint, self._default_referer(endpoint, username), "yes", ())]

    def _coerce_context(
        self,
        endpoint: str,
        context: Optional[Union[RequestContext, Dict]],
        username: Optional[str] = None,
    ) -> RequestContext:
        if isinstance(context, RequestContext):
            return context
        if isinstance(context, dict):
            return RequestContext(
                name=str(context.get("name", "custom")),
                endpoint=endpoint,
                referer=str(context.get("referer", self._default_referer(endpoint, username))),
                active_user=str(context.get("active_user", context.get("x-twitter-active-user", "yes"))),
                warmup_routes=tuple(context.get("warmup_routes", ())),
            )
        return self.get_context_variants(endpoint, username)[0]

    def _build_request_headers(
        self,
        endpoint: str,
        context: Optional[Union[RequestContext, Dict]] = None,
        username: Optional[str] = None,
        extra_headers: Optional[Dict] = None,
    ) -> Dict:
        ctx = self._coerce_context(endpoint, context, username)
        headers = dict(self.session.headers)
        headers["x-client-transaction-id"] = self._generate_transaction_id(endpoint)
        headers["referer"] = ctx.referer
        headers["x-twitter-active-user"] = ctx.active_user
        if extra_headers:
            headers.update({k: str(v) for k, v in extra_headers.items() if v})
        return headers

    def _human_delay(self, stage: str = "between_requests"):
        """Apply random delay to mimic human pacing."""
        if not self.simulation_config.get("enabled", True):
            return

        delay_map = self.simulation_config.get("delays_seconds", {
            "before_request_min": 0.05,
            "before_request_max": 0.2,
            "between_requests_min": 0.1,
            "between_requests_max": 0.4,
        })

        if stage == "before_request":
            min_d = float(delay_map.get("before_request_min", 0.2))
            max_d = float(delay_map.get("before_request_max", 1.2))
        elif stage == "between_pages":
            min_d = float(delay_map.get("between_pages_min", 0.2))
            max_d = float(delay_map.get("between_pages_max", 0.6))
        elif stage == "between_pages_replies":
            min_d = float(delay_map.get("between_pages_replies_min", 1.0))
            max_d = float(delay_map.get("between_pages_replies_max", 1.5))
        elif stage == "between_accounts":
            min_d = float(delay_map.get("between_accounts_min", 0.3))
            max_d = float(delay_map.get("between_accounts_max", 0.8))
        elif stage == "between_accounts_replies":
            min_d = float(delay_map.get("between_accounts_replies_min", 10.0))
            max_d = float(delay_map.get("between_accounts_replies_max", 12.0))
        elif stage == "replies_retry":
            min_d = float(delay_map.get("replies_retry_min", 0.1))
            max_d = float(delay_map.get("replies_retry_max", 0.3))
        else:
            min_d = float(delay_map.get("between_requests_min", 0.1))
            max_d = float(delay_map.get("between_requests_max", 0.4))

        if max_d < min_d:
            max_d = min_d
        time.sleep(random.uniform(min_d, max_d))

    def human_delay(self, stage: str = "between_requests"):
        """Public wrapper for configured anti-bot pacing."""
        self._human_delay(stage)

    def jitter_sleep(self, min_seconds: float, max_seconds: float, reason: str = "") -> float:
        """Sleep for a bounded random duration and return the delay used."""
        min_seconds = max(0.0, float(min_seconds))
        max_seconds = max(min_seconds, float(max_seconds))
        delay = random.uniform(min_seconds, max_seconds)
        if reason:
            self._info(f"{reason}; sleeping {delay:.1f}s")
        time.sleep(delay)
        return delay

    def retry_policy(self) -> Dict[str, Union[int, float]]:
        """Return configured status-aware retry settings with safe defaults."""
        configured = self.simulation_config.get("error_retry_policy", {})
        policy = configured if isinstance(configured, dict) else {}
        return {
            "client_error_attempts": int(policy.get("client_error_attempts", 3)),
            "client_error_min_seconds": float(policy.get("client_error_min_seconds", 1.0)),
            "client_error_max_seconds": float(policy.get("client_error_max_seconds", 2.0)),
            "server_error_attempts": int(policy.get("server_error_attempts", 3)),
            "server_error_base_seconds": float(policy.get("server_error_base_seconds", 1.0)),
            "server_error_max_seconds": float(policy.get("server_error_max_seconds", 4.0)),
            "request_error_attempts": int(policy.get("request_error_attempts", 3)),
            "request_error_base_seconds": float(policy.get("request_error_base_seconds", 1.0)),
            "request_error_max_seconds": float(policy.get("request_error_max_seconds", 3.0)),
            "rate_limit_safety_buffer_seconds": int(policy.get("rate_limit_safety_buffer_seconds", 1)),
            "max_rate_limit_sleep_seconds": int(policy.get("max_rate_limit_sleep_seconds", 900)),
        }

    def warmup_navigation_context(
        self,
        username: Optional[str] = None,
        endpoint: Optional[str] = None,
        context: Optional[Union[RequestContext, Dict]] = None,
    ):
        """Best-effort warmup matching the original /with_replies behavior."""
        if not self.simulation_config.get("enabled", True):
            return
        if not self.simulation_config.get("browse_warmup_enabled", False):
            return

        ctx = self._coerce_context(endpoint or "UserTweets", context, username)
        warmup_routes = list(ctx.warmup_routes)
        if not warmup_routes and username:
            warmup_routes = [
                f"https://x.com/{username}/with_replies"
                if endpoint == "UserTweetsAndReplies"
                else f"https://x.com/{username}"
            ]
        warmup_pages = int(self.simulation_config.get("warmup_pages", len(warmup_routes) or 1))
        try:
            for warmup_url in warmup_routes[:warmup_pages]:
                self._get(warmup_url)
                self._human_delay("between_requests")
        except requests.exceptions.RequestException:
            return

    def warmup_session(self, username: str) -> bool:
        """
        Human-like warm-up flow:
        1) Visit home page
        2) Visit user profile page
        3) Pin session referer to the user profile

        Dormant by default: warm-up GETs add 500-800ms with zero success-rate
        benefit (see tests/reports/COLD_VS_WARM_FINDINGS.md). Re-enable per
        deployment via simulation_config.browse_warmup_enabled=true if needed.
        """
        if not self.simulation_config.get("browse_warmup_enabled", False):
            return False
        username = (username or "").strip().lstrip("@")
        if not username:
            return False
        home_url = "https://x.com/"
        profile_url = f"https://x.com/{username}"
        try:
            self._get(home_url)
            self._get(profile_url)
            self.session.headers["referer"] = profile_url
            return True
        except requests.exceptions.RequestException as exc:
            self._warning(f"Warm-up navigation failed for @{username}: {exc}")
            return False

    def warmup_user_context(self, username: Optional[str] = None):
        """Backward-compatible profile warmup wrapper."""
        self.warmup_navigation_context(username=username, endpoint="UserTweets")

    def warmup_url(self, url: str, timeout: int = 30):
        """Best-effort warmup for non-profile routes (e.g., search pages)."""
        if not self.simulation_config.get("enabled", True):
            return
        if not self.simulation_config.get("browse_warmup_enabled", False):
            return
        target = str(url or "").strip()
        if not target:
            return
        try:
            self._get(target, timeout=timeout)
        except requests.exceptions.RequestException:
            return

    def refresh_session(self):
        """Rebuild the HTTP session while preserving durable auth/config state."""
        self.session.close()
        self.session = requests.Session()
        self._setup_session()

    def apply_browser_context(
        self,
        *,
        cookies: Optional[Dict[str, str]] = None,
        query_ids: Optional[Dict[str, str]] = None,
        request_headers: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> None:
        """Apply ephemeral Playwright-derived state without persisting secrets."""
        for name, value in (cookies or {}).items():
            if value:
                self.session.cookies.set(str(name), str(value), domain=".x.com")
        ct0 = (cookies or {}).get("ct0")
        if ct0:
            self.session.headers["x-csrf-token"] = str(ct0)
        for endpoint, query_id in (query_ids or {}).items():
            if query_id:
                self.query_ids[endpoint] = str(query_id)
                self.query_id_pools.setdefault(endpoint, [])
                if str(query_id) not in self.query_id_pools[endpoint]:
                    self.query_id_pools[endpoint].insert(0, str(query_id))
                key = self.QUERY_ID_CONFIG_KEYS.get(endpoint)
                if key:
                    self.config.setdefault("api_config", {})[key] = str(query_id)
        # Copy only the dynamic/session headers needed by the HTTP transport.
        for headers in (request_headers or {}).values():
            for key in (
                "authorization",
                "x-client-transaction-id",
                "x-csrf-token",
                "x-twitter-auth-type",
                "x-twitter-client-language",
            ):
                if headers.get(key):
                    self.session.headers[key] = str(headers[key])

    def reconcile_bootstrap_params(
        self,
        endpoint: str,
        *,
        query_ids: Optional[Dict[str, str]] = None,
        request_headers: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> None:
        """Promote browser-captured params and clear stale endpoint health after bootstrap."""
        for ep, query_id in (query_ids or {}).items():
            if query_id:
                self._mark_query_id(ep, str(query_id), "healthy")
                pool = self.query_id_pools.setdefault(ep, [])
                if str(query_id) not in pool:
                    pool.insert(0, str(query_id))
        endpoint_headers = (request_headers or {}).get(endpoint, {})
        tx_id = endpoint_headers.get("x-client-transaction-id")
        if tx_id:
            self._mark_tx_id(endpoint, str(tx_id), "healthy")
            tx_pool = self.real_tx_ids.setdefault(endpoint, [])
            if str(tx_id) not in tx_pool:
                tx_pool.insert(0, str(tx_id))
        if endpoint in (query_ids or {}):
            active = str(query_ids[endpoint])
            self.query_ids[endpoint] = active
            self.query_id_indices[endpoint] = 0
            self.consecutive_404s[endpoint] = 0
            self.endpoint_health[endpoint] = EndpointHealth.HEALTHY
            self._save_endpoint_health()

    def _save_config(self) -> None:
        """Persist current config atomically with one simple backup."""
        if self.config_path.exists():
            shutil.copy2(self.config_path, self.config_path.with_suffix(".json.bak"))
        tmp_fd, tmp_path = tempfile.mkstemp(dir=self.config_path.parent, suffix=".tmp")
        try:
            with open(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            Path(tmp_path).replace(self.config_path)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    def hot_swap_auth(self, updates: Dict[str, Any]) -> bool:
        """Apply fresh browser auth/query parameters to config and active session."""
        if not isinstance(updates, dict) or not updates:
            return False

        cookies = updates.get("api_cookies")
        if isinstance(cookies, dict) and cookies:
            self.config.setdefault("api_cookies", {}).update({k: str(v) for k, v in cookies.items() if v})

        headers = updates.get("api_headers")
        if isinstance(headers, dict) and headers:
            self.config.setdefault("api_headers", {}).update({k: str(v) for k, v in headers.items() if v})

        api_config = updates.get("api_config")
        if isinstance(api_config, dict) and api_config:
            self.config.setdefault("api_config", {}).update({k: str(v) for k, v in api_config.items() if v})

        bearer = updates.get("bearer_token")
        if bearer:
            self.config.setdefault("api_auth", {})["bearer_token"] = str(bearer)

        self._save_config()
        self.refresh_session()
        self.refresh_config_and_query_ids()
        return True
    
    def _load_rate_limits(self) -> Dict[str, Dict]:
        """Load rate limit state from disk or initialize"""
        loaded: Dict[str, Dict] = self._request_state_store().load("rate_limits.json", {})
        
        # Initialize from config or defaults
        limits = {}
        config_limits = self.config.get("rate_limits", {})
        for endpoint, default in self.DEFAULT_LIMITS.items():
            config_data = config_limits.get(endpoint, default)
            existing = loaded.get(endpoint, {}) if isinstance(loaded.get(endpoint), dict) else {}
            limit = config_data.get("limit", default["limit"])
            window_seconds = config_data.get("window_seconds", default["window_seconds"])
            limits[endpoint] = {
                "remaining": existing.get("remaining", limit),
                "reset": existing.get("reset", int(time.time()) + window_seconds),
                "limit": existing.get("limit", limit),
            }
        return limits
    
    def _save_rate_limits(self):
        """Persist rate limit state to disk"""
        self._request_state_store().save("rate_limits.json", self.rate_limits)
    
    def _load_endpoint_health(self) -> Dict[str, str]:
        """Load endpoint health status"""
        return self._request_state_store().load(
            "endpoint_health.json",
            {endpoint: EndpointHealth.HEALTHY for endpoint in self.DEFAULT_LIMITS},
        )
    
    def _save_endpoint_health(self):
        """Persist endpoint health status"""
        self._request_state_store().save("endpoint_health.json", self.endpoint_health)
    
    def _load_query_ids(self) -> Dict[str, str]:
        """Load query IDs from config"""
        api_config = self.config.get("api_config", {})
        return {
            endpoint: api_config.get(key, "")
            for endpoint, key in self.QUERY_ID_CONFIG_KEYS.items()
        }

    def _load_query_id_pools(self) -> Dict[str, List[str]]:
        pools: Dict[str, List[str]] = {}
        configured = self.config.get("query_ids_by_endpoint", {})
        configured = configured if isinstance(configured, dict) else {}
        for endpoint, current in self.query_ids.items():
            pool = configured.get(endpoint, [])
            if not isinstance(pool, list):
                pool = []
            values = [str(value) for value in pool if value]
            if current and current not in values:
                values.insert(0, current)
            pools[endpoint] = values
        return pools

    def refresh_config_and_query_ids(self):
        """Reload config + query IDs at runtime (helps after manual config updates)."""
        self.config = self._load_config()
        self.query_ids = self._load_query_ids()
        self.query_id_pools = self._load_query_id_pools()

        # If query IDs were updated in config, allow retries again.
        for endpoint in self.query_ids.keys():
            if self.endpoint_health.get(endpoint) == EndpointHealth.STALE_QUERY_ID:
                self.endpoint_health[endpoint] = EndpointHealth.HEALTHY
        self._save_endpoint_health()

    def _next_query_id(self, endpoint: str) -> Optional[str]:
        pool = self.query_id_pools.get(endpoint, [])
        state_map = self.query_id_state.get(endpoint, {})
        pinned = self.query_ids.get(endpoint)
        if pinned and (not pool or str(pinned) in pool):
            if not self._param_ruled_out(state_map.get(str(pinned))):
                return str(pinned)
        if not pool:
            return self.query_ids.get(endpoint)
        self.query_id_indices.setdefault(endpoint, 0)
        for _ in range(len(pool)):
            query_id = pool[self.query_id_indices[endpoint] % len(pool)]
            self.query_id_indices[endpoint] += 1
            if not self._param_ruled_out(state_map.get(query_id)):
                self.query_ids[endpoint] = query_id
                return query_id
        query_id = pool[-1]
        self.query_ids[endpoint] = query_id
        return query_id
    
    def check_rate_limit(self, endpoint: str, safety_margin: float = 0.9) -> Tuple[bool, Optional[int]]:
        """
        Check if we have budget for this endpoint
        
        Returns:
            (can_proceed, seconds_until_reset)
        """
        if endpoint not in self.rate_limits:
            return True, None
        
        limit_data = self.rate_limits[endpoint]
        now = int(time.time())
        
        # Check if reset time has passed
        if now >= limit_data["reset"]:
            # Reset the bucket
            limit_data["remaining"] = limit_data["limit"]
            limit_data["reset"] = now + 900  # 15 minutes
            self._save_rate_limits()
            return True, None
        
        if limit_data["remaining"] > 0:
            return True, None
        
        # Rate limited
        seconds_until_reset = limit_data["reset"] - now
        return False, seconds_until_reset

    def wait_for_rate_limit(self, endpoint: str, safety_buffer_seconds: Optional[int] = None) -> int:
        """Sleep until a persisted exhausted endpoint bucket resets."""
        if safety_buffer_seconds is None:
            safety_buffer_seconds = int(self.retry_policy().get("rate_limit_safety_buffer_seconds", 5))
        if endpoint not in self.rate_limits:
            return 0
        limit_data = self.rate_limits[endpoint]
        now = int(time.time())
        remaining = int(limit_data.get("remaining", 1) or 0)
        reset = int(limit_data.get("reset", 0) or 0)
        if remaining > 0 or reset <= now:
            return 0
        sleep_for = max(0, reset - now + int(safety_buffer_seconds))
        if sleep_for:
            self._warning(f"{endpoint} rate bucket exhausted; sleeping {sleep_for}s until reset")
            time.sleep(sleep_for)
        return sleep_for

    def seconds_until_reset(self, endpoint: str, safety_buffer_seconds: Optional[int] = None) -> int:
        """Return seconds until the persisted endpoint reset time."""
        if safety_buffer_seconds is None:
            safety_buffer_seconds = int(self.retry_policy().get("rate_limit_safety_buffer_seconds", 5))
        limit_data = self.rate_limits.get(endpoint, {})
        now = int(time.time())
        reset = int(limit_data.get("reset", 0) or 0)
        return max(0, reset - now + int(safety_buffer_seconds))

    def rate_limit_sleep_seconds(self, endpoint: str, response_headers: Optional[dict] = None) -> int:
        """Calculate bounded sleep for 429 from persisted state and response headers."""
        policy = self.retry_policy()
        wait = self.seconds_until_reset(
            endpoint,
            safety_buffer_seconds=int(policy.get("rate_limit_safety_buffer_seconds", 5)),
        )
        headers = response_headers or {}
        retry_after = headers.get("retry-after") if hasattr(headers, "get") else None
        if retry_after:
            try:
                wait = max(wait, int(float(retry_after)))
            except (TypeError, ValueError):
                pass
        max_wait = int(policy.get("max_rate_limit_sleep_seconds", 900))
        return max(0, min(wait, max_wait))
    
    def update_rate_limit(self, endpoint: str, response_headers: dict):
        """Update rate limit state from response headers"""
        if endpoint not in self.rate_limits:
            return
        
        try:
            remaining = int(response_headers.get("x-rate-limit-remaining", -1))
            reset = int(response_headers.get("x-rate-limit-reset", 0))
            limit = int(response_headers.get("x-rate-limit-limit", self.rate_limits[endpoint]["limit"]))
            
            if remaining >= 0:
                self.rate_limits[endpoint]["remaining"] = remaining
            if reset > 0:
                self.rate_limits[endpoint]["reset"] = reset
            if limit > 0:
                self.rate_limits[endpoint]["limit"] = limit
            
            self._save_rate_limits()
            
            # Check if we're hitting limits
            if remaining == 0:
                self.endpoint_health[endpoint] = EndpointHealth.RATE_LIMITED
                self._save_endpoint_health()
        except (ValueError, KeyError):
            pass

    def _query_id_from_url(self, endpoint: str, url: str) -> Optional[str]:
        try:
            parts = [part for part in urlparse(url).path.split("/") if part]
            idx = parts.index("graphql")
            if len(parts) > idx + 2 and parts[idx + 2] == endpoint:
                return parts[idx + 1]
        except Exception:
            pass
        return None
    
    def _process_response_status(
        self,
        endpoint: str,
        response: requests.Response,
        request_headers: Dict[str, Any],
        url: str,
    ) -> None:
        """Update rate-limit, health, and parameter state for a completed HTTP response."""
        self.last_status_by_endpoint[endpoint] = response.status_code
        self.update_rate_limit(endpoint, response.headers)

        if response.status_code == 200:
            self.endpoint_health[endpoint] = EndpointHealth.HEALTHY
            self._save_endpoint_health()
            current_tx_id = request_headers.get("x-client-transaction-id")
            if current_tx_id:
                self._mark_tx_id(endpoint, current_tx_id, "healthy")
            current_query_id = self._query_id_from_url(endpoint, url)
            if current_query_id:
                self._mark_query_id(endpoint, current_query_id, "healthy")
            self.consecutive_404s[endpoint] = 0
        elif response.status_code == 429:
            self.endpoint_health[endpoint] = EndpointHealth.RATE_LIMITED
            self._save_endpoint_health()
        elif response.status_code == 404:
            current_tx_id = request_headers.get("x-client-transaction-id")
            if current_tx_id:
                self._mark_tx_id(endpoint, current_tx_id, "stale")
            current_query_id = self._query_id_from_url(endpoint, url)
            if current_query_id:
                self._mark_query_id(endpoint, current_query_id, "stale")

            self.consecutive_404s[endpoint] = self.consecutive_404s.get(endpoint, 0) + 1
            self.endpoint_health[endpoint] = (
                EndpointHealth.CONTEXT_REJECTED
                if endpoint in {"UserTweetsAndReplies", "SearchTimeline"}
                else EndpointHealth.STALE_QUERY_ID
            )
            self._save_endpoint_health()
        elif 500 <= response.status_code < 600:
            self.endpoint_health[endpoint] = EndpointHealth.SERVER_ERROR
            self._save_endpoint_health()

    def perform_get(
        self,
        endpoint: str,
        url: str,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        context: Optional[Union[RequestContext, Dict]] = None,
        username: Optional[str] = None,
        **kwargs
    ) -> requests.Response:
        """
        Perform a resilient GET request and return the raw response.
        Uses adapter-level retries + bounded manual retries for request exceptions
        and 5xx responses.
        """
        extra_headers = kwargs.pop("headers", None)
        kwargs.setdefault("timeout", self.default_timeout)

        last_exception: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                self.wait_for_rate_limit(endpoint)
                self._human_delay("before_request")
                self.request_count += 1
                request_headers = self._build_request_headers(
                    endpoint,
                    context=context,
                    username=username,
                    extra_headers=extra_headers,
                )
                response = self._get(url, headers=request_headers, **kwargs)
                self._process_response_status(endpoint, response, request_headers, url)

                if response.status_code == 200:
                    return response
                if response.status_code == 429:
                    return response
                if response.status_code == 404:
                    # Trigger auto-refresh after 3 consecutive 404s if all candidate params are ruled out.
                    if self.consecutive_404s[endpoint] >= 3 and (
                        self._all_tx_ids_stale(endpoint) or self._all_query_ids_stale(endpoint)
                    ):
                        self._warning(
                            f"{endpoint} hit {self.consecutive_404s[endpoint]} consecutive 404s; "
                            f"checking auto-refresh for @{username or 'default'}"
                        )
                        if self._auto_refresh_params(endpoint, username=username):
                            self._info(f"Auto-refresh successful; retrying {endpoint} for @{username or 'default'}")
                            request_headers = self._build_request_headers(endpoint, context, username, extra_headers)
                            response = self._get(url, headers=request_headers, **kwargs)
                            self._process_response_status(endpoint, response, request_headers, url)
                            if response.status_code == 200:
                                return response
                            if response.status_code == 404:
                                self._warning(
                                    f"Auto-refresh did not recover {endpoint} for @{username or 'default'}; "
                                    "skipping further refresh loops for this account/endpoint."
                                )
                            return response
                        self._error(f"Auto-refresh failed for {endpoint} for @{username or 'default'}")
                    return response
                if 500 <= response.status_code < 600:
                    if attempt < max_retries - 1:
                        wait = retry_delay * (2 ** attempt)
                        self._warning(f"{response.status_code} on {endpoint}; retrying in {wait:.1f}s")
                        time.sleep(wait)
                        continue

                return response
            except requests.exceptions.RequestException as exc:
                last_exception = exc
                if attempt < max_retries - 1:
                    wait = retry_delay * (2 ** attempt)
                    self._warning(f"Request error on {endpoint}: {exc}; retrying in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                raise

        if last_exception:
            raise last_exception
        raise RuntimeError(f"Request loop exited unexpectedly for endpoint={endpoint}")

    def _auto_refresh_params(self, endpoint: str, username: Optional[str] = None) -> bool:
        """
        Launch auto-refresh via Playwright to get fresh tx-ids + query-ids.
        Returns True on success.
        """
        key = (str(endpoint), str(username or "__default__"))
        if key in self.auto_refresh_attempts:
            self._warning(
                f"Auto-refresh already attempted for {endpoint} for @{username or 'default'}; "
                "skipping another refresh loop."
            )
            return False

        self.auto_refresh_attempts[key] = 1
        recorder = getattr(self, "recorder", None)
        if recorder:
            recorder.emit_auto_refresh_start(
                trigger="candidate_pool_exhausted",
                endpoint=endpoint,
                username=username,
            )

        try:
            from tweeter_data_fetcher.x_api.auth import auto_refresh_session
        except ImportError:
            self._error("Auto-refresh module not available")
            if recorder:
                recorder.emit_auto_refresh_done(
                    endpoint=endpoint,
                    updated=[],
                    success=False,
                    username=username,
                )
            return False
        
        success = auto_refresh_session(self.config_path, endpoints=[endpoint])
        if not success:
            if recorder:
                recorder.emit_auto_refresh_done(
                    endpoint=endpoint,
                    updated=[],
                    success=False,
                    username=username,
                )
            return False
        
        # Reload config and tx-id pools
        self.config = getattr(self, "config", {})
        self.refresh_config_and_query_ids()
        self.real_tx_ids = self.config.get("real_transaction_ids_by_endpoint", {})
        if not self.real_tx_ids:
            self.real_tx_ids = {"_default": self.config.get("real_transaction_ids", [])}
        
        # Reset tx-id state for this endpoint (mark all new tx-ids as healthy)
        if endpoint in self.real_tx_ids:
            self.tx_id_state[endpoint] = {tx_id: "healthy" for tx_id in self.real_tx_ids[endpoint]}
            self._save_tx_id_state()
        if endpoint in self.query_id_pools:
            self.query_id_state[endpoint] = {
                query_id: {"status": "healthy", "failures": 0}
                for query_id in self.query_id_pools[endpoint]
            }
            self._save_query_id_state()
        if recorder:
            recorder.emit_auto_refresh_done(
                endpoint=endpoint,
                updated=[endpoint],
                success=True,
                username=username,
            )
        
        return True
    
    def make_request(
        self,
        endpoint: str,
        url: str,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        context: Optional[Union[RequestContext, Dict]] = None,
        username: Optional[str] = None,
        **kwargs
    ) -> Optional[requests.Response]:
        """
        Backward-compatible request helper returning None on non-successful states.
        
        Args:
            endpoint: Endpoint name for rate limiting
            url: Full URL to request
            max_retries: Maximum retry attempts for 5xx errors
            retry_delay: Base delay between retries (exponential backoff)
            **kwargs: Additional arguments for requests.get()
        
        Returns:
            Response object or None if failed
        """
        try:
            response = self.perform_get(
                endpoint=endpoint,
                url=url,
                max_retries=max_retries,
                retry_delay=retry_delay,
                context=context,
                username=username,
                **kwargs,
            )
        except requests.exceptions.RequestException as e:
            self._error(f"Request failed on {endpoint} after {max_retries} attempts: {e}")
            return None

        if response.status_code == 200:
            return response

        if response.status_code == 404:
            self._warning(f"404 on {endpoint}: request context or query ID rejected")
            return None

        if response.status_code == 429:
            retry_after = int(response.headers.get("retry-after", 900))
            self._warning(f"Rate limited on {endpoint}; retry after {retry_after}s")
            return None

        if 500 <= response.status_code < 600:
            return None

        self._warning(f"Unexpected status {response.status_code} on {endpoint}")
        return None
    
    def get_query_id(self, endpoint: str) -> Optional[str]:
        """Get query ID for an endpoint"""
        # Keep synced with config for long-running sessions / manual edits.
        self.refresh_config_and_query_ids()
        return self._next_query_id(endpoint)
    
    def get_endpoint_health(self, endpoint: str) -> str:
        """Get health status of an endpoint"""
        return self.endpoint_health.get(endpoint, EndpointHealth.UNKNOWN_ERROR)

    def get_last_status(self, endpoint: str) -> Optional[int]:
        """Get the last HTTP status observed for an endpoint."""
        return self.last_status_by_endpoint.get(endpoint)
    
    def get_stats(self) -> dict:
        """Get session statistics"""
        elapsed = time.time() - self.session_start
        return {
            "requests_made": self.request_count,
            "session_duration_seconds": int(elapsed),
            "requests_per_minute": round(self.request_count / (elapsed / 60), 2) if elapsed > 0 else 0,
            "rate_limits": self.rate_limits,
            "endpoint_health": self.endpoint_health,
        }
