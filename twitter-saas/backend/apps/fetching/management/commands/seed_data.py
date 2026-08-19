"""Seed TwitterUser rows (from seed/accounts.json tier config) and Search rows
(from seed/searches.json) so a fresh install has tracked accounts and searches.

Idempotent: re-running upserts by handle/slug and never duplicates.

Usage:
    python manage.py seed_data
    python manage.py seed_data --no-track   # create accounts but don't poll them
"""
from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.tweets.models import Search, TwitterUser

SEED_DIR = Path(settings.BASE_DIR) / "seed"


class Command(BaseCommand):
    help = "Seed TwitterUser and Search rows from the bundled seed JSON files."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--no-track",
            action="store_true",
            help="Create accounts with tracking disabled.",
        )

    def handle(self, *args, **options) -> None:
        track = not options["no_track"]
        accounts = self._seed_accounts(track)
        searches = self._seed_searches()
        self.stdout.write(
            self.style.SUCCESS(f"Seeded {accounts} accounts, {searches} searches.")
        )

    def _seed_accounts(self, track: bool) -> int:
        path = SEED_DIR / "accounts.json"
        if not path.exists():
            self.stdout.write(self.style.WARNING(f"missing {path}"))
            return 0
        data = json.loads(path.read_text(encoding="utf-8"))
        count = 0
        # accounts.json is {"priority_N": [{"username","display_name"}, ...], ...}.
        for key, value in data.items():
            if not isinstance(value, list):
                continue
            try:
                priority = max(1, min(7, int(str(key).split("_", 1)[1])))
            except (IndexError, ValueError):
                priority = 7
            for entry in value:
                handle = str(entry.get("username") or "").lstrip("@").strip().lower()
                if not handle:
                    continue
                TwitterUser.objects.update_or_create(
                    handle=handle,
                    defaults={
                        "display_name": entry.get("display_name") or "",
                        "tracking": track,
                        "priority": priority,
                    },
                )
                count += 1
        return count

    def _seed_searches(self) -> int:
        path = SEED_DIR / "searches.json"
        if not path.exists():
            self.stdout.write(self.style.WARNING(f"missing {path}"))
            return 0
        data = json.loads(path.read_text(encoding="utf-8"))
        count = 0
        for entry in data if isinstance(data, list) else []:
            slug = str(entry.get("slug") or "").strip()
            raw_query = entry.get("raw_query")
            if not slug or not raw_query:
                continue
            product = entry.get("product") or "Top"
            Search.objects.update_or_create(
                slug=slug,
                product=product,
                defaults={
                    "name": entry.get("name") or slug,
                    "raw_query": raw_query,
                    "pagination_depth": max(1, int(entry.get("pagination_depth", 1) or 1)),
                    "rolling_hours": max(1, int(entry.get("rolling_hours", 24) or 24)),
                    "enabled": bool(entry.get("enabled", True)),
                },
            )
            count += 1
        return count
