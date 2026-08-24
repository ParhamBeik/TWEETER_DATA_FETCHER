"""How the three fetch buckets spent a recent window.

    python manage.py fetch_report --since 24h

This is the soak instrument from the quota-aware scheduling plan: requests
spent, accounts polled vs due, tweets first-seen vs upserted, archive
completion, and every account still walking.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tweets.models import EndpointState, FetchRun, Search, Tweet, TwitterUser

_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_since(spec: str) -> timedelta:
    text = spec.strip().lower()
    if len(text) < 2 or text[-1] not in _UNITS or not text[:-1].isdigit():
        raise ValueError(f"expected Nh/Nm/Nd, got {spec!r}")
    return timedelta(**{_UNITS[text[-1]]: int(text[:-1])})


def _summary(run: FetchRun) -> dict:
    return run.summary if isinstance(run.summary, dict) else {}


def _pages(run: FetchRun) -> int:
    summary = _summary(run)
    if summary.get("raw_pages"):
        return int(summary["raw_pages"] or 0)
    return int((summary.get("event_counts") or {}).get("page_fetched") or 0)


def _upserted(run: FetchRun) -> int:
    return int(_summary(run).get("ingested_tweets") or 0)


def _report_int(run: FetchRun, key: str) -> int:
    total = 0
    for report in _summary(run).get("reports") or []:
        if isinstance(report, dict):
            total += int(report.get(key) or 0)
    return total


def _run_accounts(run: FetchRun) -> list[str]:
    for event in _summary(run).get("recent_events") or []:
        if isinstance(event, dict) and event.get("type") == "run_start":
            accounts = event.get("accounts")
            if isinstance(accounts, list):
                return [str(a).lstrip("@").lower() for a in accounts]
    target = str(run.target or "")
    if target and not target.startswith("chunk:"):
        return [target.lstrip("@").lower()]
    return []


def build_report(*, since, now=None) -> dict[str, Any]:
    """Aggregate FetchRun / Tweet / EndpointState into one soak snapshot."""
    now = now or timezone.now()
    runs = list(FetchRun.objects.filter(started_at__gte=since).order_by("started_at"))
    tracked = list(TwitterUser.objects.filter(tracking=True))
    tracked_handles = {u.handle.lower() for u in tracked}

    def bucket(subsystem: str, endpoint: str) -> dict[str, Any]:
        subset = [r for r in runs if r.subsystem == subsystem]
        first_seen = Tweet.objects.filter(
            ingested_at__gte=since, source_endpoint=endpoint
        ).count()
        upserted = sum(_upserted(r) for r in subset)
        polled: set[str] = set()
        for run in subset:
            polled.update(_run_accounts(run))
        return {
            "runs": len(subset),
            "pages": sum(_pages(r) for r in subset),
            "upserted": upserted,
            "first_seen": first_seen,
            "reupserted": max(0, upserted - first_seen),
            "polled": sorted(polled),
            "deferred": sum(_report_int(r, "deferred") for r in subset),
            "statuses": {
                status: sum(1 for r in subset if r.status == status)
                for status in ("completed", "partial", "failed", "running", "auth_required")
                if any(r.status == status for r in subset)
            },
        }

    archive: dict[str, dict] = {}
    for row in EndpointState.objects.filter(endpoint="UserTweets"):
        data = row.data if isinstance(row.data, dict) else {}
        archive[str(row.account).lower()] = data

    complete = [
        handle for handle in tracked_handles if archive.get(handle, {}).get("backfill_complete")
    ]
    walking = []
    for user in tracked:
        handle = user.handle.lower()
        state = archive.get(handle, {})
        if state.get("backfill_complete"):
            continue
        walking.append(
            {
                "handle": user.handle,
                "pages": int(state.get("backfill_pages_done") or 0),
                "outcome": state.get("backfill_last_outcome") or "not_started",
            }
        )
    walking.sort(key=lambda row: (-int(row["pages"]), row["handle"].lower()))

    live = bucket("live", "UserTweets")
    # Live and historical share UserTweets, so first_seen would double-count if
    # both buckets read the same queryset. Attribute first-seen timeline rows to
    # historical when the tweet is older than the live window (a few hours);
    # everything newer is the fresh bucket.
    live_horizon = now - timedelta(hours=6)
    live["first_seen"] = Tweet.objects.filter(
        ingested_at__gte=since,
        source_endpoint="UserTweets",
        created_at__gte=live_horizon,
    ).count()
    live["reupserted"] = max(0, live["upserted"] - live["first_seen"])

    historical = bucket("historical", "UserTweets")
    historical["first_seen"] = Tweet.objects.filter(
        ingested_at__gte=since,
        source_endpoint="UserTweets",
        created_at__lt=live_horizon,
    ).count() + Tweet.objects.filter(
        ingested_at__gte=since,
        source_endpoint="UserTweets",
        created_at__isnull=True,
    ).count()
    historical["reupserted"] = max(0, historical["upserted"] - historical["first_seen"])

    search = bucket("search", "SearchTimeline")
    searches = [
        {
            "slug": s.slug,
            "product": s.product,
            "enabled": s.enabled,
            "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
        }
        for s in Search.objects.all().order_by("slug", "product")
    ]

    return {
        "now": now.isoformat(),
        "since": since.isoformat(),
        "tracked": len(tracked),
        "live": live,
        "historical": historical,
        "search": {**search, "queries": searches},
        "archive": {
            "complete": len(complete),
            "tracked": len(tracked),
            "walking": walking,
        },
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        f"fetch_report  since {report['since']}  now {report['now']}",
        f"tracked accounts: {report['tracked']}",
        "",
    ]
    for name in ("live", "historical", "search"):
        bucket = report[name]
        lines.append(name.upper())
        lines.append(
            f"  runs={bucket['runs']}  pages={bucket['pages']}  "
            f"upserted={bucket['upserted']}  first_seen={bucket['first_seen']}  "
            f"reupserted={bucket['reupserted']}"
        )
        lines.append(
            f"  polled={len(bucket['polled'])}  deferred={bucket['deferred']}  "
            f"statuses={bucket['statuses'] or '-'}"
        )
        if name == "search":
            for query in bucket.get("queries") or []:
                flag = "on" if query["enabled"] else "off"
                lines.append(
                    f"  {query['slug']} [{query['product']}] {flag} last={query['last_run_at'] or 'never'}"
                )
        lines.append("")
    archive = report["archive"]
    lines.append(
        f"ARCHIVE  complete={archive['complete']}/{archive['tracked']}"
    )
    if archive["walking"]:
        lines.append("  still walking:")
        for row in archive["walking"]:
            lines.append(f"    @{row['handle']}  {row['pages']}p  {row['outcome']}")
    else:
        lines.append("  every tracked account is fully archived")
    return "\n".join(lines)


class Command(BaseCommand):
    help = "Print how live, historical, and search spent a recent window."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--since",
            default="24h",
            help="Window to summarise, like 24h, 6h, or 30m. Default 24h.",
        )

    def handle(self, *args, **options) -> None:
        try:
            delta = parse_since(options["since"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        now = timezone.now()
        self.stdout.write(render(build_report(since=now - delta, now=now)))
