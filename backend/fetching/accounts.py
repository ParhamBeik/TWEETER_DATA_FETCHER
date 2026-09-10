"""Materialize DB accounts into CLI accounts.json and read engine live state."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from itertools import pairwise
from typing import Any

from django.conf import settings
from django.db.models import Count, Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from fetcher.config import DEFAULT_PRIORITY_POLICIES

from tweets.models import EndpointState, KeyValueState, Tweet, TwitterUser

# Mirrors fetcher.historical.DEPTH_PROVIDER_LIMIT without importing the pipeline.
PROVIDER_DEPTH_LIMIT = "provider_depth_limit"
_EXHAUSTED = "success_timeline_exhausted"


def clamp_priority(value: Any) -> int:
    try:
        return max(1, min(7, int(value)))
    except (TypeError, ValueError):
        return 7


def policy_for(priority: int) -> dict:
    return dict(DEFAULT_PRIORITY_POLICIES[clamp_priority(priority)])


# An account needs at least this many timestamped tweets in the measurement
# window before its median gap means anything; below it the tier default is a
# better guess than a sample of two.
MIN_SAMPLE_TWEETS = 6


def median_gap_seconds(times: list) -> int | None:
    """Median seconds between consecutive posts, or None on too small a sample.

    Median rather than mean because posting is bursty: a single overnight gap
    would drag a mean far past anything the account actually does.
    """
    ordered = sorted(t for t in times if t is not None)
    if len(ordered) < MIN_SAMPLE_TWEETS:
        return None
    gaps = sorted(
        (later - earlier).total_seconds() for earlier, later in pairwise(ordered)
    )
    middle = len(gaps) // 2
    value = gaps[middle] if len(gaps) % 2 else (gaps[middle - 1] + gaps[middle]) / 2
    return int(value)


def interval_for(priority: int, gap_seconds: int | None) -> int:
    """Polling interval for an account: its own rate, clamped into its tier's band.

    The tier expresses how much this account matters, the measurement expresses
    how much there is to collect. Importance wins at the edges -- a priority-1
    account that goes quiet is still checked hourly, and a chatty priority-7 one
    cannot poll its way past a priority-1.
    """
    policy = policy_for(priority)
    low = int(policy["poll_interval_min_seconds"])
    high = int(policy["poll_interval_max_seconds"])
    if not gap_seconds:
        return int(policy["poll_interval_seconds"])
    return max(low, min(high, int(gap_seconds)))


# One spare request so a 429 still has somewhere to land. The archive floor
# already reserved one seat per due live account; stacking a larger reserve
# on top of that leftover is how 20 "for live" became 15 slots.
LIVE_RATE_RESERVE = 1
USER_TWEETS_WINDOW_LIMIT = 50


def archive_quota_floor(due_count: int, *, reserve: int = LIVE_RATE_RESERVE, limit: int = USER_TWEETS_WINDOW_LIMIT) -> int:
    """Requests the archive walk must leave in the shared UserTweets bucket.

    One seat per live account that is already due, plus live's own reserve,
    never more than the window. When nobody is due this collapses to the
    reserve, which is the leftover the archive is allowed to spend.
    """
    return min(int(limit), max(0, int(due_count)) + int(reserve))


def tracked_accounts_payload(cap: int | None = None) -> dict:
    """CLI accounts.json buckets from tracked TwitterUser rows."""
    limit = cap if cap is not None else settings.FETCH_MAX_ACCOUNTS_PER_RUN
    qs = TwitterUser.objects.filter(tracking=True).order_by("priority", "id")
    if limit:
        qs = qs[: int(limit)]
    buckets = {f"priority_{index}": [] for index in range(1, 8)}
    for user in qs:
        priority = clamp_priority(user.priority)
        record = {
            "username": user.handle,
            "display_name": user.display_name or user.handle,
        }
        # Only measured cadences travel; an account without one falls back to
        # its tier default inside the engine rather than carrying a duplicate.
        if user.poll_interval_seconds:
            record["poll_interval_seconds"] = int(user.poll_interval_seconds)
        # The raw posting rate, alongside the interval derived from it. The
        # interval answers "how often to look"; the live poller separately needs
        # "how much will be waiting when I do" to size its page budget, and the
        # interval cannot answer that -- it is clamped into the tier's band, so
        # every priority-1 account reports the same number no matter how much it
        # actually posts.
        if user.observed_median_gap_seconds:
            record["observed_median_gap_seconds"] = int(user.observed_median_gap_seconds)
        buckets[f"priority_{priority}"].append(record)
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


def due_live_handles(*, now=None) -> list[str]:
    """Tracked accounts the live poller would admit as due right now.

    Same inputs the engine uses (last_checked_at vs the account's interval).
    The archive walk sizes its quota floor from this list so it cannot spend
    the seats those accounts need.
    """
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, dt_timezone.utc)
    live = live_state_map()
    due: list[str] = []
    for user in TwitterUser.objects.filter(tracking=True, quarantined=False).order_by("priority", "id"):
        state = live.get(user.handle.lower(), {})
        if state.get("quarantined") is True:
            continue
        interval = int(user.poll_interval_seconds or policy_for(user.priority)["poll_interval_seconds"])
        last = _parse_when(state.get("last_checked_at"))
        if last is None or (now - last).total_seconds() >= interval:
            due.append(user.handle)
    return due


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


def archive_state() -> dict[str, dict]:
    """Per-account archive-walk state blob, keyed by lowercased handle.

    The engine normalizes handles to lowercase when it writes sync state; the
    TwitterUser table preserves the display casing. Joining in Python beats an
    iexact subquery for a fleet this size and keeps one obvious mapping.
    """
    return {
        str(row.account).lower(): (row.data if isinstance(row.data, dict) else {})
        for row in EndpointState.objects.filter(endpoint="UserTweets")
    }


def archive_progress() -> dict:
    """How far the finite backward walk has got, per tracked account.

    One definition shared by the pipeline API and `manage.py fetch_report`, so
    the console and the CLI cannot disagree about what "fully archived" means.
    """
    archive = archive_state()
    complete: list[str] = []
    # Split out of `complete` on purpose: these accounts are done being walked,
    # but only because X stopped serving, not because we reached their first
    # tweet. Counting them as complete is what made "every tracked account is
    # fully archived" true on paper while @elonmusk held three months of history.
    depth_limited: list[str] = []
    walking: list[dict] = []
    for user in TwitterUser.objects.filter(tracking=True).order_by("priority", "handle"):
        state = archive.get(user.handle.lower(), {})
        if state.get("backfill_complete"):
            if _is_provider_depth(state):
                depth_limited.append(user.handle)
            else:
                complete.append(user.handle)
            continue
        walking.append({
            "handle": user.handle,
            "priority": user.priority,
            "pages": int(state.get("backfill_pages_done") or 0),
            "stalled_ticks": int(state.get("backfill_stalled_ticks") or 0),
            "outcome": state.get("backfill_last_outcome") or "not_started",
            "quarantined": bool(user.quarantined),
        })
    # Most-advanced first, so the accounts closest to done lead the list.
    walking.sort(key=lambda row: (-row["pages"], row["handle"].lower()))
    return {
        "complete": complete,
        "depth_limited": depth_limited,
        "walking": walking,
        "tracked": len(complete) + len(depth_limited) + len(walking),
    }


def _is_provider_depth(state: dict) -> bool:
    """X stopped serving, not 'we have the first tweet'.

    Rows written before backfill_depth_reason existed stored
    success_timeline_exhausted as complete with no reason -- treating those as
    fully archived is the original lie.
    """
    if state.get("backfill_depth_reason") == PROVIDER_DEPTH_LIMIT:
        return True
    return (
        not state.get("backfill_depth_reason")
        and state.get("backfill_last_outcome") == _EXHAUSTED
    )


DEPTH_REPROBE_DAYS = 30


def due_depth_probes(archive: dict | None = None, *, now=None) -> list[str]:
    """Parked-at-the-wall accounts whose last verdict is old enough to re-test.

    A probe is only cheap if the walk left a cursor at the wall. Without one,
    retrying would start from the top of the timeline -- a full re-walk, which
    is the thing parking was meant to stop. Accounts already parked before
    the cursor was kept therefore stay parked.
    """
    archive = archive if archive is not None else archive_state()
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, dt_timezone.utc)
    due: list[str] = []
    for handle, state in archive.items():
        if not state.get("backfill_complete"):
            continue
        if not _is_provider_depth(state):
            continue
        cursor = state.get("backfill_cursor")
        if not cursor or cursor in {"__START__", "__END__"}:
            continue
        completed = _parse_when(state.get("backfill_completed_at"))
        if completed is not None and (now - completed) < timedelta(days=DEPTH_REPROBE_DAYS):
            continue
        due.append(handle)
    return due


# How many of an account's own posting gaps may pass with nothing collected
# before the silence is worth reporting, and the floor under that for accounts
# that post many times an hour.
SILENCE_GAP_MULTIPLE = 12
SILENCE_MIN_HOURS = 6


def silent_accounts(*, now=None) -> list[dict]:
    """Tracked accounts we keep polling successfully that have gone quiet.

    Every fetch of @realdonaldtrump for 41 days reported `completed`, because
    completeness only ever asked whether pagination had walked back *past* the
    window start -- which a page of stale tweets satisfies trivially. Nothing
    asked the one question an operator cares about: is anything still arriving.
    The poll is not broken and there is nothing to retry; what was missing is the
    signal, so this is a measurement rather than a fix to the walk.

    Silence is judged against each account's own measured cadence instead of a
    flat number of days: six hours without a post is alarming for @reuters and
    unremarkable for a tier-7 account that posts twice a month.
    """
    now = now or timezone.now()
    live = live_state_map()
    users = list(TwitterUser.objects.filter(tracking=True, quarantined=False))
    newest = {
        str(row["account"]).lower(): row["newest"]
        for row in Tweet.objects.filter(
            account__in=[user.handle.lower() for user in users]
        ).values("account").annotate(newest=Max("created_at"))
    }

    silent: list[dict] = []
    for user in users:
        key = user.handle.lower()
        state = live.get(key, {})
        # Only accounts the poller believes it is handling. One that has never
        # been checked is a different problem and would drown this signal.
        last_checked = _parse_when(state.get("last_checked_at"))
        if last_checked is None:
            continue
        cadence = int(
            user.observed_median_gap_seconds
            or user.poll_interval_seconds
            or policy_for(user.priority)["poll_interval_seconds"]
        )
        tolerance = timedelta(
            seconds=max(cadence * SILENCE_GAP_MULTIPLE, SILENCE_MIN_HOURS * 3600)
        )
        latest = newest.get(key)
        quiet_for = None if latest is None else now - latest
        if latest is not None and quiet_for < tolerance:
            continue
        silent.append({
            "handle": user.handle,
            "priority": user.priority,
            "last_tweet_at": latest,
            "quiet_hours": None if quiet_for is None else round(quiet_for.total_seconds() / 3600, 1),
            "tolerance_hours": round(tolerance.total_seconds() / 3600, 1),
            "last_checked_at": last_checked,
            "last_status": state.get("last_status") or "",
        })
    # Longest silence first, and an account that has never produced a single
    # tweet leads: there is no "quiet for N hours" to rank it by, and it is the
    # worse case.
    silent.sort(key=lambda row: (row["quiet_hours"] is not None, -(row["quiet_hours"] or 0)))
    return silent


RECENT_TWEET_HOURS = 24


def watermark_map(handles: list[str]) -> dict[str, dict]:
    """Per-account endpoint watermarks, keyed by lowercased handle, in one query.

    The per-account spelling was an `__iexact` filter run once per row, which is
    what made the unpaginated roster cost two queries per account. The engine
    lowercases handles when it writes state, so lowering both sides here is the
    same match without the per-row round trip.
    """
    wanted = {handle.lower() for handle in handles}
    if not wanted:
        return {}
    grouped: dict[str, dict] = {}
    for row in EndpointState.objects.filter(account__in=wanted):
        data = row.data if isinstance(row.data, dict) else {}
        grouped.setdefault(str(row.account).lower(), {})[row.endpoint] = (
            data.get("fetch_watermark") or data.get("watermark")
        )
    return grouped


def recent_tweet_counts(handles: list[str]) -> dict[str, int]:
    """Posts per account in the last RECENT_TWEET_HOURS, in one grouped query."""
    wanted = [handle.lower() for handle in handles]
    if not wanted:
        return {}
    since = timezone.now() - timedelta(hours=RECENT_TWEET_HOURS)
    rows = (
        Tweet.objects.filter(account__in=wanted, created_at__gte=since)
        .order_by()
        .values("account")
        .annotate(total=Count("id"))
    )
    return {str(row["account"]).lower(): int(row["total"]) for row in rows}


def account_ops(
    user: TwitterUser,
    live: dict[str, dict] | None = None,
    *,
    watermarks: dict[str, dict] | None = None,
    recent_counts: dict[str, int] | None = None,
) -> dict:
    """Schedule + health fields derived from CLI policy and live_state.

    `watermarks` and `recent_counts` are the page-wide maps a list view resolves
    once (see the helpers above). Both fall back to a single-account query so a
    bare `account_ops(user)` still works anywhere.
    """
    policy = policy_for(user.priority)
    key = user.handle.lower()
    # `is None`, not a truthiness check: an empty map is a legitimate answer
    # ("the engine has not written live state yet"), and treating it as absent
    # made every row of the roster re-run the lookup that the caller had already
    # done once precisely so it would not have to.
    if live is None:
        live = live_state_map()
    state = live.get(key, {})
    last_checked = _parse_when(state.get("last_checked_at"))
    interval = int(user.poll_interval_seconds or policy["poll_interval_seconds"])
    if watermarks is None:
        watermarks = watermark_map([user.handle])
    if recent_counts is None:
        recent_counts = recent_tweet_counts([user.handle])
    return {
        "poll_interval_seconds": interval,
        "observed_median_gap_seconds": user.observed_median_gap_seconds,
        "live_window_hours": policy["live_window_hours"],
        "historical_window_days": policy["historical_window_days"],
        "last_checked_at": last_checked,
        "last_status": state.get("last_status") or "",
        "recent_tweet_count": int(recent_counts.get(key, 0)),
        "watermarks": watermarks.get(key, {}),
    }


