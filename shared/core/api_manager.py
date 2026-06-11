#!/usr/bin/env python3
"""
API Manager - Centralized networking and endpoint management

Responsibilities:
- Session management
- Authentication (cookies, bearer, CSRF)
- Query ID management with auto-refresh detection
- Per-endpoint rate limit tracking
- Retry logic with exponential backoff
- Request accounting and budgeting
- Endpoint health monitoring
"""

import json
import time
import uuid
import base64
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util import Retry
except ImportError:
    print("ERROR: Missing requests. Run: pip3 install requests")
    raise


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

    def __init__(self, config_path: str = "config/config.json", state_dir: Optional[Path] = None):
        project_root = Path(__file__).resolve().parent.parent
        path_obj = Path(config_path)
        if not path_obj.is_absolute():
            path_obj = project_root / path_obj
        self.config_path = path_obj
        self.config = self._load_config()
        self.simulation_config = self.config.get("anti_bot_simulation", {})
        self.default_timeout = int(
            self.config.get("api_config", {}).get("default_timeout_seconds", 20)
        )
        
        # State directory for persistent tracking
        self.state_dir = state_dir or (project_root / "data" / "state")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.shared_state_dir = project_root / "data" / "state"
        self.shared_state_dir.mkdir(parents=True, exist_ok=True)
        
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

        # Request accounting
        self.request_count = 0
        self.session_start = time.time()
        
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

        # Stable browser/session headers copied from the original reliable
        # fetcher. Route-specific requests override only referer/active-user.
        self.session.headers.update({
            "authorization": f"Bearer {bearer}",
            "x-csrf-token": csrf_token,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "x-client-transaction-id": tx_id,
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            "referer": "https://x.com/",
            "accept": "*/*",
            "content-type": "application/json",
            "dnt": "1",
            "priority": "u=1, i",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
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
    
    def _generate_transaction_id(self) -> str:
        """Generate one fallback transaction ID for the session."""
        raw = uuid.uuid4().bytes + int(time.time() * 1000).to_bytes(8, 'big')
        return base64.urlsafe_b64encode(raw).decode()[:72]

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
                RequestContext("replies_tab_passive", endpoint, replies_url, "no", (replies_url,)),
                RequestContext("replies_tab_active", endpoint, replies_url, "yes", (replies_url,)),
                RequestContext("home_fallback", endpoint, "https://x.com/", "yes", ()),
            ]

        if endpoint == "UserTweets":
            return [RequestContext("user_profile", endpoint, f"https://x.com/i/user/{username}" if username else profile_url, "yes", ())]

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
        headers["referer"] = ctx.referer
        headers["x-twitter-active-user"] = ctx.active_user
        if extra_headers:
            headers.update({k: str(v) for k, v in extra_headers.items() if v})
        if endpoint == "UserTweetsAndReplies":
            headers = self._apply_replies_request_profile(headers, username)
        return headers

    def _apply_replies_request_profile(self, headers: Dict, username: Optional[str]) -> Dict:
        """Match the standalone UserTweetsAndReplies diagnostic request shape."""
        cookies = self.config.get("api_cookies", {})
        bearer = self.config.get("api_auth", {}).get("bearer_token", "")
        csrf_token = cookies.get("ct0", "")
        configured_headers = self.config.get("api_headers", {})
        tx_id = configured_headers.get("x-client-transaction-id") or headers.get("x-client-transaction-id")
        referer = f"https://x.com/{username}/with_replies" if username else "https://x.com/"

        cookie_bits = []
        for key in ["auth_token", "ct0", "lang"]:
            value = cookies.get(key)
            if value:
                cookie_bits.append(f"{key}={value}")

        headers.update({
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "en-GB,en;q=0.9,es-ES;q=0.8,es;q=0.7,en-US;q=0.6",
            "authorization": f"Bearer {bearer}",
            "content-type": "application/json",
            "cookie": "; ".join(cookie_bits),
            "dnt": "1",
            "priority": "u=1, i",
            "referer": referer,
            "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "x-csrf-token": csrf_token,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
        })
        if tx_id:
            headers["x-client-transaction-id"] = str(tx_id)
        return headers

    def _human_delay(self, stage: str = "between_requests"):
        """Apply random delay to mimic human pacing."""
        if not self.simulation_config.get("enabled", True):
            return

        delay_map = self.simulation_config.get("delays_seconds", {
            "before_request_min": 0.2,
            "before_request_max": 1.2,
            "between_requests_min": 0.5,
            "between_requests_max": 2.5,
        })

        if stage == "before_request":
            min_d = float(delay_map.get("before_request_min", 0.2))
            max_d = float(delay_map.get("before_request_max", 1.2))
        elif stage == "between_pages":
            min_d = float(delay_map.get("between_pages_min", 2))
            max_d = float(delay_map.get("between_pages_max", 6))
        elif stage == "between_accounts":
            min_d = float(delay_map.get("between_accounts_min", 3))
            max_d = float(delay_map.get("between_accounts_max", 8))
        elif stage == "replies_retry":
            min_d = float(delay_map.get("replies_retry_min", 1))
            max_d = float(delay_map.get("replies_retry_max", 3))
        else:
            min_d = float(delay_map.get("between_requests_min", 0.5))
            max_d = float(delay_map.get("between_requests_max", 2.5))

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
            print(f"⏳ {reason}; sleeping {delay:.1f}s")
        time.sleep(delay)
        return delay

    def retry_policy(self) -> Dict[str, Union[int, float]]:
        """Return configured status-aware retry settings with safe defaults."""
        configured = self.simulation_config.get("error_retry_policy", {})
        policy = configured if isinstance(configured, dict) else {}
        return {
            "client_error_attempts": int(policy.get("client_error_attempts", 3)),
            "client_error_min_seconds": float(policy.get("client_error_min_seconds", 10)),
            "client_error_max_seconds": float(policy.get("client_error_max_seconds", 20)),
            "server_error_attempts": int(policy.get("server_error_attempts", 3)),
            "server_error_base_seconds": float(policy.get("server_error_base_seconds", 5)),
            "server_error_max_seconds": float(policy.get("server_error_max_seconds", 60)),
            "request_error_attempts": int(policy.get("request_error_attempts", 3)),
            "request_error_base_seconds": float(policy.get("request_error_base_seconds", 5)),
            "request_error_max_seconds": float(policy.get("request_error_max_seconds", 60)),
            "rate_limit_safety_buffer_seconds": int(policy.get("rate_limit_safety_buffer_seconds", 5)),
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
        if not self.simulation_config.get("browse_warmup_enabled", True):
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
        """
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
            print(f"⚠️  Warm-up navigation failed for @{username}: {exc}")
            return False

    def warmup_user_context(self, username: Optional[str] = None):
        """Backward-compatible profile warmup wrapper."""
        self.warmup_navigation_context(username=username, endpoint="UserTweets")

    def warmup_url(self, url: str, timeout: int = 30):
        """Best-effort warmup for non-profile routes (e.g., search pages)."""
        if not self.simulation_config.get("enabled", True):
            return
        if not self.simulation_config.get("browse_warmup_enabled", True):
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
    
    def _load_rate_limits(self) -> Dict[str, Dict]:
        """Load rate limit state from disk or initialize"""
        state_file = self.shared_state_dir / "rate_limits.json"
        loaded: Dict[str, Dict] = {}
        if state_file.exists():
            try:
                with open(state_file) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    loaded = data
            except:
                pass
        
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
        state_file = self.shared_state_dir / "rate_limits.json"
        with open(state_file, 'w') as f:
            json.dump(self.rate_limits, f, indent=2)
    
    def _load_endpoint_health(self) -> Dict[str, str]:
        """Load endpoint health status"""
        state_file = self.state_dir / "endpoint_health.json"
        if state_file.exists():
            try:
                with open(state_file) as f:
                    return json.load(f)
            except:
                pass
        return {endpoint: EndpointHealth.HEALTHY for endpoint in self.DEFAULT_LIMITS.keys()}
    
    def _save_endpoint_health(self):
        """Persist endpoint health status"""
        state_file = self.state_dir / "endpoint_health.json"
        with open(state_file, 'w') as f:
            json.dump(self.endpoint_health, f, indent=2)
    
    def _load_query_ids(self) -> Dict[str, str]:
        """Load query IDs from config"""
        api_config = self.config.get("api_config", {})
        return {
            "UserByScreenName": api_config.get("user_by_screen_name_query_id", "sLVLhk0bGj3MVFEKTdax1w"),
            "UserTweets": api_config.get("user_tweets_query_id", "pQHADmT91zIY83UbK0x4Lw"),
            "UserTweetsAndReplies": api_config.get("user_tweets_and_replies_query_id", "6eh3huj6fJnA3Naupj4w0Q"),
            "TweetDetail": api_config.get("tweet_detail_query_id", ""),
            "SearchTimeline": api_config.get("search_timeline_query_id", ""),
        }

    def refresh_config_and_query_ids(self):
        """Reload config + query IDs at runtime (helps after manual config updates)."""
        self.config = self._load_config()
        self.query_ids = self._load_query_ids()

        # If query IDs were updated in config, allow retries again.
        for endpoint in self.query_ids.keys():
            if self.endpoint_health.get(endpoint) == EndpointHealth.STALE_QUERY_ID:
                self.endpoint_health[endpoint] = EndpointHealth.HEALTHY
        self._save_endpoint_health()
    
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
        
        # Check if we have remaining budget
        threshold = int(limit_data["limit"] * safety_margin)
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
            print(f"⏳ {endpoint} rate bucket exhausted; sleeping {sleep_for}s until reset.")
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
                self.last_status_by_endpoint[endpoint] = response.status_code
                self.update_rate_limit(endpoint, response.headers)

                if response.status_code == 200:
                    self.endpoint_health[endpoint] = EndpointHealth.HEALTHY
                    self._save_endpoint_health()
                elif response.status_code == 429:
                    self.endpoint_health[endpoint] = EndpointHealth.RATE_LIMITED
                    self._save_endpoint_health()
                elif response.status_code == 404:
                    self.endpoint_health[endpoint] = (
                        EndpointHealth.CONTEXT_REJECTED
                        if endpoint in {"UserTweetsAndReplies", "SearchTimeline"}
                        else EndpointHealth.STALE_QUERY_ID
                    )
                    self._save_endpoint_health()
                elif 500 <= response.status_code < 600:
                    if attempt < max_retries - 1:
                        wait = retry_delay * (2 ** attempt)
                        print(f"⚠️  {response.status_code} on {endpoint}, retrying in {wait:.1f}s...")
                        time.sleep(wait)
                        continue
                    self.endpoint_health[endpoint] = EndpointHealth.SERVER_ERROR
                    self._save_endpoint_health()

                return response
            except requests.exceptions.RequestException as exc:
                last_exception = exc
                if attempt < max_retries - 1:
                    wait = retry_delay * (2 ** attempt)
                    print(f"⚠️  Request error on {endpoint}: {exc}, retrying in {wait:.1f}s...")
                    time.sleep(wait)
                    continue
                raise

        if last_exception:
            raise last_exception
        raise RuntimeError(f"Request loop exited unexpectedly for endpoint={endpoint}")

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
            print(f"✗ Request failed on {endpoint} after {max_retries} attempts: {e}")
            return None

        if response.status_code == 200:
            return response

        if response.status_code == 404:
            print(f"⚠️  404 on {endpoint} - request context or query ID rejected")
            return None

        if response.status_code == 429:
            retry_after = int(response.headers.get("retry-after", 900))
            print(f"⏳ Rate limited on {endpoint}, retry after {retry_after}s")
            return None

        if 500 <= response.status_code < 600:
            return None

        print(f"⚠️  Unexpected status {response.status_code} on {endpoint}")
        return None
    
    def get_query_id(self, endpoint: str) -> Optional[str]:
        """Get query ID for an endpoint"""
        # Keep synced with config for long-running sessions / manual edits.
        self.refresh_config_and_query_ids()
        return self.query_ids.get(endpoint)
    
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
