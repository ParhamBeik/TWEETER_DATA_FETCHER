"""Validation and safe status helpers for the shared X session."""
from __future__ import annotations

from apps.tweets.models import FetchRun, XSession


def validate_session_payload(data) -> tuple[dict[str, str], dict[str, str]]:
    if not isinstance(data, dict):
        raise ValueError("session payload must be a JSON object")
    cookies = data.get("cookies", {})
    headers = data.get("headers", {})
    cookies = {} if cookies is None else cookies
    headers = {} if headers is None else headers
    if not isinstance(cookies, dict) or not isinstance(headers, dict):
        raise ValueError("`cookies` and `headers` must be JSON objects")
    return (
        {str(key): str(value) for key, value in cookies.items() if value},
        {str(key): str(value) for key, value in headers.items() if value},
    )


# Config keys the engine reads from config.json that are bound to a captured
# browser session, so they belong on XSession (Postgres) rather than the tracked
# seed template. Anything else in a pasted payload is ignored.
SESSION_CONFIG_KEYS = (
    "real_transaction_ids_by_endpoint",
    "query_ids_by_endpoint",
    "api_config",
)


def normalize_session_source(data) -> dict:
    """Accept either a session JSON or a raw engine config.json.

    The engine's config.json stores credentials as `api_cookies`/`api_headers`
    with the bearer under `api_auth.bearer_token`, while the session shape uses
    `cookies`/`headers` with an `authorization` header. Operators paste both, so
    normalize here once rather than at each call site.
    """
    if not isinstance(data, dict):
        return data
    if "cookies" in data or "headers" in data:
        return data
    cookies = data.get("api_cookies")
    headers = dict(data.get("api_headers") or {})
    if cookies is None and not headers:
        return data
    bearer = str((data.get("api_auth") or {}).get("bearer_token") or "").strip()
    if bearer and not any(key.lower() == "authorization" for key in headers):
        headers["authorization"] = bearer if bearer.lower().startswith("bearer ") else f"Bearer {bearer}"
    csrf = str((cookies or {}).get("ct0") or "").strip()
    if csrf and not any(key.lower() == "x-csrf-token" for key in headers):
        headers["x-csrf-token"] = csrf
    return {**data, "cookies": cookies or {}, "headers": headers}


def validate_config_overrides(data) -> dict:
    """Pick the session-bound config keys out of a full config.json payload."""
    if not isinstance(data, dict):
        return {}
    overrides = {}
    for key in SESSION_CONFIG_KEYS:
        value = data.get(key)
        if isinstance(value, dict) and value:
            overrides[key] = value
    return overrides


def session_health() -> dict:
    session = XSession.objects.filter(active=True).first()
    last_auth_failure = FetchRun.objects.filter(status="auth_required").first()
    overrides = (session.config_overrides or {}) if session else {}
    tx_pools = overrides.get("real_transaction_ids_by_endpoint") or {}
    return {
        "configured": session is not None and bool(session.cookies or session.headers),
        "active": bool(session),
        "cookie_names": sorted(session.cookies) if session else [],
        "header_names": sorted(session.headers) if session else [],
        # Counts only -- a captured tx-id is a session credential, never returned.
        "transaction_id_pools": {
            key: len(value) for key, value in sorted(tx_pools.items()) if isinstance(value, list)
        },
        "updated_at": session.updated_at if session else None,
        "last_auth_required_at": last_auth_failure.started_at if last_auth_failure else None,
    }
