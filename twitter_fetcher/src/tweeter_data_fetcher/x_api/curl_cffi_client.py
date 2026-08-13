#!/usr/bin/env python3
"""
curl_cffi transport for Twitter/X GraphQL endpoints.

Why curl_cffi: real HTTP/2 multiplexing cuts median latency ~4x vs the
requests library on the same authenticated calls (measured: pooled 424ms vs
requests 1771ms). That is the only reason this module exists.

What this module deliberately does NOT do:
  - No connection warm-up. A warm-up GET to x.com adds 500-800ms per session
    for zero success-rate benefit (proven in tests/reports/COLD_VS_WARM_FINDINGS.md:
    cold == warm_naive == warm_pooled on success). The latency win comes from
    reusing a persistent HTTP/2 Session, not from a priming request.
  - No hand-rolled header set. Request headers are built by the canonical
    APIManager._build_request_headers (one source of truth). A divergent
    hand-rolled header set was the actual root cause of the UserTweetsAndReplies
    404s — not TLS, not warm-up. See COLD_VS_WARM_FINDINGS.md §3.

Public surface:
  - CurlCffiAPIManager: drop-in replacement path mirroring APIManager.perform_get.
  - CurlCffiSession: one persistent curl_cffi Session reused across requests.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    print("ERROR: Missing curl_cffi. Run: pip install curl_cffi")
    raise

logger = logging.getLogger(__name__)

# curl_cffi >=0.13 reports response.http_version as an int ALPN code.
_ALPN = {0: "HTTP/1.0", 1: "HTTP/1.1", 2: "SPDY", 3: "HTTP/2", 4: "HTTP/3"}


class CurlCffiResponse:
    """Thin wrapper preserving the fields the pipelines read from requests.Response."""

    def __init__(self, raw_response: Any, elapsed_seconds: float):
        self.raw = raw_response
        self.status_code = raw_response.status_code
        self.text = raw_response.text
        self.content = raw_response.content
        self.headers = dict(raw_response.headers)
        self.cookies = getattr(raw_response, "cookies", {})
        self.url = raw_response.url
        self.elapsed_seconds = elapsed_seconds
        ver = getattr(raw_response, "http_version", "HTTP/1.1")
        self.http_version = _ALPN.get(ver, str(ver)) if isinstance(ver, int) else str(ver)

    def json(self) -> Any:
        return json.loads(self.text)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class CurlCffiSession:
    """
    One persistent curl_cffi.requests.Session, reused across requests.

    Reuse (keep-alive + HTTP/2 multiplexing) is the entire latency win.
    Cookies are seeded once from config; the canonical header builder supplies
    auth/csrf/referer per request.
    """

    def __init__(self, endpoint: str, config: Dict[str, Any], impersonate: str = "chrome120"):
        self.endpoint = endpoint
        self.impersonate = impersonate
        self.request_count = 0
        self.last_request_at: Optional[float] = None
        self.response_times: list = []
        self.status_codes: Dict[int, int] = {}

        self.session = cffi_requests.Session(impersonate=impersonate)
        cookies = config.get("api_cookies", {}) or {}
        for name, value in cookies.items():
            if value:
                self.session.cookies.set(name, str(value), domain=".x.com")
        logger.debug("curl_cffi session for %s ready (impersonate=%s)", endpoint, impersonate)

    def get(self, url: str, headers: Dict[str, str], timeout: int = 20, **kwargs) -> CurlCffiResponse:
        start = time.time()
        response = self.session.get(url, headers=headers, timeout=timeout, **kwargs)
        elapsed = time.time() - start
        self.request_count += 1
        self.last_request_at = time.time()
        self.response_times.append(elapsed)
        self.status_codes[response.status_code] = self.status_codes.get(response.status_code, 0) + 1
        logger.debug(
            "[%s] %s -> %s (%.2fs, %s)",
            self.endpoint, url.split("?")[0][-48:], response.status_code, elapsed, self.http_version(response),
        )
        return CurlCffiResponse(response, elapsed)

    @staticmethod
    def http_version(response: Any) -> str:
        ver = getattr(response, "http_version", "HTTP/1.1")
        return _ALPN.get(ver, str(ver)) if isinstance(ver, int) else str(ver)

    def stats(self) -> Dict[str, Any]:
        avg = sum(self.response_times) / len(self.response_times) if self.response_times else 0
        return {
            "endpoint": self.endpoint,
            "impersonate": self.impersonate,
            "request_count": self.request_count,
            "avg_response_time_ms": round(avg * 1000, 1),
            "status_codes": self.status_codes,
        }

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            pass


class CurlCffiAPIManager:
    """
    Drop-in curl_cffi transport mirroring the requests-based APIManager.perform_get.

    Headers are always built by the canonical APIManager._build_request_headers so
    curl_cffi can never drift from the proven recipe. The persistent curl_cffi
    Session is created lazily per endpoint and reused for every subsequent call.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        console: Optional[Any] = None,
        recorder: Optional[Any] = None,
        impersonate: str = "chrome120",
    ):
        from tweeter_data_fetcher.configuration import resolve_config_path

        self.config_path = resolve_config_path(config_path)
        with open(self.config_path) as f:
            self.config = json.load(f)
        self.console = console
        self.recorder = recorder
        self.impersonate = impersonate
        self.default_timeout = int(self.config.get("api_config", {}).get("default_timeout_seconds", 20))

        self._sessions: Dict[str, CurlCffiSession] = {}
        # Lazily-created canonical requests manager used only to build request
        # headers (one source of truth — never hand-roll the header set).
        self._header_manager = None
        self.request_count = 0
        logger.info("CurlCffiAPIManager initialized (impersonate=%s)", impersonate)

    def _info(self, msg: str) -> None:
        if self.console:
            self.console.info(msg)
        else:
            logger.info(msg)

    def _warning(self, msg: str) -> None:
        if self.console:
            self.console.warning(msg)
        else:
            logger.warning(msg)

    def _canonical_request_headers(
        self,
        endpoint: str,
        context: Optional[Union[Dict, Any]] = None,
        username: Optional[str] = None,
    ) -> Dict[str, str]:
        from tweeter_data_fetcher.x_api.client import APIManager

        if self._header_manager is None:
            self._header_manager = APIManager(config_path=str(self.config_path), console=self.console)
        return self._header_manager._build_request_headers(endpoint, context=context, username=username)

    def _session_for(self, endpoint: str) -> CurlCffiSession:
        sess = self._sessions.get(endpoint)
        if sess is None:
            sess = CurlCffiSession(endpoint=endpoint, config=self.config, impersonate=self.impersonate)
            self._sessions[endpoint] = sess
        return sess

    def perform_get(
        self,
        endpoint: str,
        url: str,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        context: Optional[Union[Dict, Any]] = None,
        username: Optional[str] = None,
        **kwargs,
    ) -> CurlCffiResponse:
        """
        GET via curl_cffi. Mirrors APIManager.perform_get's retry contract:
        retries only transport exceptions and 5xx; returns 200/404/429 as-is.
        """
        extra_headers = kwargs.pop("headers", None)
        timeout = kwargs.pop("timeout", self.default_timeout)
        session = self._session_for(endpoint)
        headers = self._canonical_request_headers(endpoint, context, username)
        if extra_headers:
            headers.update({k: str(v) for k, v in extra_headers.items() if v})

        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                self.request_count += 1
                response = session.get(url, headers=headers, timeout=timeout, **kwargs)
                if response.status_code == 200:
                    self._info(f"✓ {endpoint} 200 ({response.http_version})")
                    return response
                if response.status_code in (404, 429):
                    self._warning(f"{response.status_code} on {endpoint} for @{username or 'default'}")
                    return response
                if 500 <= response.status_code < 600 and attempt < max_retries - 1:
                    wait = retry_delay * (2 ** attempt)
                    self._warning(f"{response.status_code} on {endpoint}; retrying in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                return response
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < max_retries - 1:
                    wait = retry_delay * (2 ** attempt)
                    self._warning(f"Transport error on {endpoint}: {exc}; retrying in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                raise

        if last_exc:
            raise last_exc
        raise RuntimeError(f"Request loop exited unexpectedly for endpoint={endpoint}")

    def get_session_stats(self) -> Dict[str, Any]:
        return {ep: sess.stats() for ep, sess in self._sessions.items()}

    def cleanup(self) -> None:
        for sess in self._sessions.values():
            sess.close()
        self._sessions.clear()

    def reset_transport_session(self, endpoint: Optional[str] = None, *, reason: str = "rotation") -> None:
        """Drop curl_cffi pooled sessions so the next request opens a fresh HTTP/2 socket."""
        if endpoint:
            sess = self._sessions.pop(endpoint, None)
            if sess is not None:
                sess.close()
                self._info(f"Reset curl_cffi session for {endpoint} ({reason})")
            return
        self.cleanup()
        self._info(f"Reset all curl_cffi sessions ({reason})")
