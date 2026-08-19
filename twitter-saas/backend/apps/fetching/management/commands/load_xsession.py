"""Load the single shared operator X session (cookies + headers) into the one
XSession row. App users never provide X credentials; the operator runs this.

Usage:
    python manage.py load_xsession --file /path/to/session.json
    python manage.py load_xsession            # reads $X_SESSION_JSON

The JSON file/env value is shaped:
    {"cookies": {"auth_token": "...", "ct0": "..."}, "headers": {"authorization": "Bearer ...", "x-csrf-token": "..."}}
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.fetching.session import (
    normalize_session_source,
    validate_config_overrides,
    validate_session_payload,
)
from apps.tweets.models import XSession


class Command(BaseCommand):
    help = "Load or replace the shared X session (cookies/headers) in Postgres."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--file", help="Path to a session JSON file.")
        parser.add_argument("--name", default="default", help="Session row name.")
        parser.add_argument(
            "--deactivate-others",
            action="store_true",
            help="Mark all other session rows inactive so only this one is used.",
        )

    def handle(self, *args, **options) -> None:
        raw: str | None = None
        if options["file"]:
            path = Path(options["file"])
            if not path.exists():
                raise CommandError(f"session file not found: {path}")
            raw = path.read_text(encoding="utf-8")
        else:
            raw = os.environ.get("X_SESSION_JSON")
        if not raw:
            raise CommandError("provide --file or set X_SESSION_JSON")

        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise CommandError(f"invalid session JSON: {exc}") from exc

        data = normalize_session_source(data)
        try:
            cookies, headers = validate_session_payload(data)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        overrides = validate_config_overrides(data)

        defaults = {"cookies": cookies, "headers": headers, "active": True}
        if overrides:
            defaults["config_overrides"] = overrides
        session, created = XSession.objects.update_or_create(
            name=options["name"], defaults=defaults,
        )
        if options["deactivate_others"]:
            XSession.objects.exclude(pk=session.pk).update(active=False)

        verb = "created" if created else "updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"X session '{session.name}' {verb}: "
                f"{len(cookies)} cookies, {len(headers)} headers, "
                f"overrides={sorted(overrides)}, active={session.active}"
            )
        )
