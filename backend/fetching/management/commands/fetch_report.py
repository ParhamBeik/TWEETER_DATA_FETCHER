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

from fetching.accounts import archive_progress

from tweets.models import FetchRun, Search, Tweet, TwitterUser

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
    tracked = TwitterUser.objects.filter(tracking=True).count()

    def bucket(subsystem: str) -> dict[str, Any]:
        subset = [r for r in runs if r.subsystem == subsystem]
        # Tweet.source_subsystem records which pipeline first captured a row, so
        # live and historical no longer have to be told apart by guessing from
        # how old the tweet is. Rows ingested before that column existed carry
        # "" and are counted by neither bucket.
        first_seen = Tweet.objects.filter(
            ingested_at__gte=since, source_subsystem=subsystem
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

    progress = archive_progress()

    live = bucket("live")
    historical = bucket("historical")
    search = bucket("search")
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
        "tracked": tracked,
        "live": live,
        "historical": historical,
        "search": {**search, "queries": searches},
        "archive": {
            "complete": len(progress["complete"]),
            "tracked": progress["tracked"],
            "walking": progress["walking"],
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
