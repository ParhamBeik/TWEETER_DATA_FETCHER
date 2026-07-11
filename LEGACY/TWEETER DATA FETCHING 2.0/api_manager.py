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
    }
    
    # Default rate limits (overridden by config)
    DEFAULT_LIMITS = {
        "UserByScreenName": {"limit": 150, "window_seconds": 900},
        "UserTweets": {"limit": 50, "window_seconds": 900},
        "UserTweetsAndReplies": {"limit": 500, "window_seconds": 900},
        "TweetDetail": {"limit": 150, "window_seconds": 900},
    }

    def __init__(self, config_path: str = "config.json", state_dir: Optional[Path] = None):
        path_obj = Path(config_path)
        if not path_obj.is_absolute():
            path_obj = Path(__file__).parent / path_obj
        self.config_path = path_obj
        self.config = self._load_config()
        self.simulation_config = self.config.get("anti_bot_simulation", {})
        
        # State directory for persistent tracking
        self.state_dir = state_dir or (Path(__file__).parent / "data" / "STATE")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        
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
        else:
            min_d = float(delay_map.get("between_requests_min", 0.5))
            max_d = float(delay_map.get("between_requests_max", 2.5))

        if max_d < min_d:
            max_d = min_d
        time.sleep(random.uniform(min_d, max_d))

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

        if not username:
            return
        warmup_url = (
            f"https://x.com/{username}/with_replies"
            if endpoint == "UserTweetsAndReplies"
            else f"https://x.com/{username}"
        )
        try:
            self.session.get(warmup_url, timeout=30)
        except requests.exceptions.RequestException:
            return

    def warmup_user_context(self, username: Optional[str] = None):
        """Backward-compatible profile warmup wrapper."""
        self.warmup_navigation_context(username=username, endpoint="UserTweets")

    def refresh_session(self):
        """Rebuild the HTTP session while preserving durable auth/config state."""
        self.session.close()
        self.session = requests.Session()
        self._setup_session()
    
    def _load_rate_limits(self) -> Dict[str, Dict]:
        """Load rate limit state from disk or initialize"""
        state_file = self.state_dir / "rate_limits.json"
        if state_file.exists():
            try:
                with open(state_file) as f:
                    return json.load(f)
            except:
                pass
        
        # Initialize from config or defaults
        limits = {}
        config_limits = self.config.get("rate_limits", {})
        for endpoint, default in self.DEFAULT_LIMITS.items():
            config_data = config_limits.get(endpoint, default)
            limits[endpoint] = {
                "remaining": config_data.get("limit", default["limit"]),
                "reset": int(time.time()) + config_data.get("window_seconds", default["window_seconds"]),
                "limit": config_data.get("limit", default["limit"]),
            }
        return limits
    
    def _save_rate_limits(self):
        """Persist rate limit state to disk"""
        state_file = self.state_dir / "rate_limits.json"
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
        Make an API request with retry logic and rate limit handling
        
        Args:
            endpoint: Endpoint name for rate limiting
            url: Full URL to request
            max_retries: Maximum retry attempts for 5xx errors
            retry_delay: Base delay between retries (exponential backoff)
            **kwargs: Additional arguments for requests.get()
        
        Returns:
            Response object or None if failed
        """
        extra_headers = kwargs.pop("headers", None)
        
        # Make request with retries
        for attempt in range(max_retries):
            try:
                self.request_count += 1
                request_headers = self._build_request_headers(
                    endpoint,
                    context=context,
                    username=username,
                    extra_headers=extra_headers,
                )
                response = self.session.get(url, headers=request_headers, **kwargs)
                self.last_status_by_endpoint[endpoint] = response.status_code
                
                # Update rate limits from headers
                self.update_rate_limit(endpoint, response.headers)
                
                # Handle different status codes
                if response.status_code == 200:
                    self.endpoint_health[endpoint] = EndpointHealth.HEALTHY
                    self._save_endpoint_health()
                    return response
                
                elif response.status_code == 404:
                    # Replies 404s are often route/fingerprint rejection rather
                    # than a true missing GraphQL operation.
                    self.endpoint_health[endpoint] = (
                        EndpointHealth.CONTEXT_REJECTED
                        if endpoint == "UserTweetsAndReplies"
                        else EndpointHealth.STALE_QUERY_ID
                    )
                    self._save_endpoint_health()
                    print(f"⚠️  404 on {endpoint} - request context or query ID rejected")
                    return None
                
                elif response.status_code == 429:
                    # Rate limited
                    self.endpoint_health[endpoint] = EndpointHealth.RATE_LIMITED
                    self._save_endpoint_health()
                    retry_after = int(response.headers.get("retry-after", 900))
                    print(f"⏳ Rate limited on {endpoint}, retry after {retry_after}s")
                    return None
                
                elif 500 <= response.status_code < 600:
                    # Server error - retry with backoff
                    if attempt < max_retries - 1:
                        wait = retry_delay * (2 ** attempt)
                        print(f"⚠️  {response.status_code} on {endpoint}, retrying in {wait:.1f}s...")
                        time.sleep(wait)
                        continue
                    else:
                        self.endpoint_health[endpoint] = EndpointHealth.SERVER_ERROR
                        self._save_endpoint_health()
                        return None
                
                else:
                    # Other error
                    print(f"⚠️  Unexpected status {response.status_code} on {endpoint}")
                    return None
                    
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    wait = retry_delay * (2 ** attempt)
                    print(f"⚠️  Request error on {endpoint}: {e}, retrying in {wait:.1f}s...")
                    time.sleep(wait)
                    continue
                else:
                    print(f"✗ Request failed on {endpoint} after {max_retries} attempts: {e}")
                    return None
        
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
