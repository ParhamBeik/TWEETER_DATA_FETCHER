"""One home for "which time range, which accounts".

Three surfaces ask the same two questions of a request: what period is this
about, and which handles. The API analytics views, the feed, the CSV exporter
and `manage.py fetch_report` all need the same answers, and they were reaching
across layers to get them -- `tweets.analytics` imported `parse_since` out of a
management command, and `fetching.exports` imported `normalize_handles` back
out of `tweets.analytics`. Both of those are transport modules importing each
other; the shared vocabulary belongs somewhere neither owns.

Nothing here touches the database or the ORM: it turns request parameters into
plain values. The window contract itself is documented on `tweets.analytics`,
which is the surface that publishes it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

from django.utils import timezone
from django.utils.dateparse import parse_datetime

# The archive can outlive any window we chart, but a 90-day hourly scan is the
# point where these queries stop being interactive. Cap rather than let a
# hand-edited URL table-scan the whole tweet table.
MAX_WINDOW_HOURS = 24 * 90
DEFAULT_RANGE = "24h"
# date_trunc/Trunc kinds we allow. Never interpolate a raw request value into
# SQL -- membership in this dict is what makes the raw-SQL views injection-safe.
BUCKET_KINDS = {"hour": "hour", "day": "day", "week": "week"}
AUTO_BUCKET_HOUR_LIMIT = 48

# Bounds for any request-supplied instant. `window_from` subtracts a window
# length from `until` and `Window.previous_since` subtracts another from
# `since`, so an instant near datetime.min made those raise OverflowError --
# `?until=0001-01-01` was an uncaught 500 on every analytics endpoint.
# Clamping rather than rejecting keeps the existing contract that a nonsense
# window resolves to a usable one; both bounds are far outside anything X has
# ever hosted, so a clamped request returns nothing, which is the honest answer
# to "what happened in the year 1".
EARLIEST_INSTANT = datetime(1900, 1, 1, tzinfo=dt_timezone.utc)
LATEST_INSTANT = datetime(2200, 1, 1, tzinfo=dt_timezone.utc)

_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_since(spec: str) -> timedelta:
    """A window spec as a timedelta, or ValueError -- and only ever ValueError.

    `isdigit` accepts a number of any length while timedelta tops out around
    2.7 million years, so the constructor was the second way out of this
    function. It raised OverflowError, which neither caller catches: the API's
    `?range=99999999999d` reached the analytics views as an uncaught 500.
    """
    text = spec.strip().lower()
    if len(text) < 2 or text[-1] not in _UNITS or not text[:-1].isdigit():
        raise ValueError(f"expected Nh/Nm/Nd, got {spec!r}")
    try:
        return timedelta(**{_UNITS[text[-1]]: int(text[:-1])})
    except OverflowError as exc:
        raise ValueError(f"{spec!r} is longer than any representable period") from exc


@dataclass(frozen=True)
class Window:
    since: datetime
    until: datetime
    bucket: str

    @property
    def hours(self) -> float:
        return (self.until - self.since).total_seconds() / 3600

    @property
    def previous_since(self) -> datetime:
        """Start of the equal-length period immediately before this one."""
        return self.since - (self.until - self.since)


def parse_instant(value) -> datetime | None:
    """One request-supplied timestamp as an aware datetime, or None if unusable.

    Shared with the feed, which needs the same spelling of "an ISO timestamp the
    console might send" -- including the space-separated offset a URL-decoded
    `+` turns into.
    """
    if not value:
        return None
    raw = str(value).replace("Z", "+00:00")
    parsed = parse_datetime(raw)
    if parsed is None and " " in raw:
        raw_fixed = re.sub(r"\s([0-9]{2}:[0-9]{2})$", r"+\1", raw)
        parsed = parse_datetime(raw_fixed)
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def _representable(value: datetime) -> datetime:
    """Keep an instant inside the range the window arithmetic can hold."""
    return min(max(value, EARLIEST_INSTANT), LATEST_INSTANT)


def window_from(request) -> Window:
    """Resolve range/since/until/bucket into one clamped, validated window."""
    until = _representable(
        parse_instant(request.query_params.get("until")) or timezone.now()
    )
    since = parse_instant(request.query_params.get("since"))
    if since is not None:
        since = _representable(since)
    if since is None:
        try:
            span = parse_since(request.query_params.get("range") or DEFAULT_RANGE)
        except ValueError:
            span = parse_since(DEFAULT_RANGE)
        # Capped before the subtraction rather than after it. `?range=999999999d`
        # is a perfectly representable timedelta of 2.7 million years, and it is
        # `until - span` itself that overflows; clamping the result afterwards
        # never runs. The cap is the same one applied below, so this changes no
        # window that was already legal.
        since = until - min(span, timedelta(hours=MAX_WINDOW_HOURS))
    if since >= until:
        since = until - parse_since(DEFAULT_RANGE)
    since = max(since, until - timedelta(hours=MAX_WINDOW_HOURS))

    bucket = str(request.query_params.get("bucket") or "auto").lower()
    if bucket not in BUCKET_KINDS:
        span_hours = (until - since).total_seconds() / 3600
        bucket = "hour" if span_hours <= AUTO_BUCKET_HOUR_LIMIT else "day"
    return Window(since=since, until=until, bucket=bucket)


def normalize_handles(values) -> list[str]:
    """Normalize a list of ?account= values the way handles are stored.

    Also accepts one comma-joined value, which is what a URL built from a
    multi-select naturally produces. Shared with the feed so both surfaces spell
    the account filter the same way.
    """
    handles = [part for value in values or [] for part in str(value).split(",")]
    return sorted({h.strip().lstrip("@").lower() for h in handles if h.strip()})


def accounts_from(request) -> list[str]:
    """Repeatable ?account= filter, normalized the way handles are stored."""
    return normalize_handles(request.query_params.getlist("account"))
