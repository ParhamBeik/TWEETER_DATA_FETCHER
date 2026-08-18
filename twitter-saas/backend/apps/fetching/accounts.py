"""Materialize DB accounts into CLI accounts.json and read engine live state."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from django.conf import settings
from django.db.models import Count
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from tweeter_data_fetcher.account_config import DEFAULT_PRIORITY_POLICIES

from apps.tweets.models import EndpointState, KeyValueState, Tweet, TwitterUser


def clamp_priority(value: Any) -> int:
    try:
        return max(1, min(7, int(value)))
    except (TypeError, ValueError):
        return 7


def policy_for(priority: int) -> dict:
    return dict(DEFAULT_PRIORITY_POLICIES[clamp_priority(priority)])


def tracked_accounts_payload(cap: int | None = None) -> dict:
    """CLI accounts.json buckets from tracked TwitterUser rows."""
    limit = cap if cap is not None else settings.FETCH_MAX_ACCOUNTS_PER_RUN
    qs = TwitterUser.objects.filter(tracking=True).order_by("priority", "id")
    if limit:
        qs = qs[: int(limit)]
    buckets = {f"priority_{index}": [] for index in range(1, 8)}
    for user in qs:
        priority = clamp_priority(user.priority)
        buckets[f"priority_{priority}"].append(
            {
                "username": user.handle,
                "display_name": user.display_name or user.handle,
            }
        )
    return buckets


def live_state_map() -> dict[str, dict]:
    row = (
        KeyValueState.objects.filter(
            namespace="request_state", name="historical_live:live_state.json"
        ).first()
        or KeyValueState.objects.filter(namespace="request_state", name="live_state.json").first()
    )
    if row is None or not isinstance(row.data, dict):
        return {}
    return {
        str(handle).lower(): state
        for handle, state in row.data.items()
        if isinstance(state, dict) and not str(handle).startswith("_")
    }


def _parse_when(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = parse_datetime(str(value).replace("Z", "+00:00"))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def sync_quarantine_from_live_state() -> int:
    """Copy CLI user-ID quarantine onto TwitterUser. One definition, two stores."""
    updated = 0
    for handle, state in live_state_map().items():
        if "quarantined" not in state and "quarantine_reason" not in state:
            continue
        quarantined = bool(state.get("quarantined"))
        count = TwitterUser.objects.filter(handle__iexact=handle).update(
            quarantined=quarantined,
            quarantine_reason=str(state.get("quarantine_reason") or "")[:255],
            quarantined_at=_parse_when(state.get("quarantined_at")) if quarantined else None,
        )
        updated += count
    return updated


def clear_live_quarantine(handle: str) -> None:
    key = handle.lower().lstrip("@")
    row = (
        KeyValueState.objects.filter(
            namespace="request_state", name="historical_live:live_state.json"
        ).first()
        or KeyValueState.objects.filter(namespace="request_state", name="live_state.json").first()
    )
    if row is None or not isinstance(row.data, dict):
        return
    current = dict(row.data)
    account = dict(current.get(key) or {})
    account["quarantined"] = False
    account["quarantine_reason"] = ""
    account["availability_failure_count"] = 0
    account.pop("quarantined_at", None)
    current[key] = account
    row.data = current
    row.save(update_fields=["data", "updated_at"])


def account_ops(user: TwitterUser, live: dict[str, dict] | None = None) -> dict:
    """Schedule + health fields derived from CLI policy and live_state."""
    policy = policy_for(user.priority)
    state = (live or live_state_map()).get(user.handle.lower(), {})
    last_checked = _parse_when(state.get("last_checked_at"))
    interval = int(policy["poll_interval_seconds"])
    watermarks = {}
    for row in EndpointState.objects.filter(account__iexact=user.handle):
        data = row.data if isinstance(row.data, dict) else {}
        watermarks[row.endpoint] = data.get("fetch_watermark") or data.get("watermark")
    recent_since = timezone.now() - timedelta(hours=24)
    return {
        "poll_interval_seconds": interval,
        "live_window_hours": policy["live_window_hours"],
        "historical_window_days": policy["historical_window_days"],
        "last_checked_at": last_checked,
        "last_status": state.get("last_status") or "",
        "recent_tweet_count": Tweet.objects.filter(
            account=user.handle, created_at__gte=recent_since
        ).count(),
        "watermarks": watermarks,
    }


def recent_tweet_counts(handles: list[str]) -> dict[str, int]:
    rows = (
        Tweet.objects.filter(account__in=handles)
        .values("account")
        .annotate(total=Count("id"))
    )
    return {row["account"]: row["total"] for row in rows}
