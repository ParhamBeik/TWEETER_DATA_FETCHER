"""Ask X how deep an account's timeline really goes, without touching state.

`run_fetcher` is the wrong tool for a diagnosis: it opens a FetchRun, persists
sync state, endpoint state and raw pages, and would let a probe overwrite the
very cursors being investigated. This builds the same scratch root and session
config, runs `fetcher.probe` against it, and deletes the directory -- so the only
lasting effect is the output on stdout.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fetching.redaction import _literal_secrets, redact_text
from fetching.runner import SCRATCH_PREFIX, _restore_state, _write_config


class Command(BaseCommand):
    help = "Read-only depth probe of one account's UserTweets timeline."

    def add_arguments(self, parser):
        parser.add_argument("account")
        parser.add_argument("--pages", type=int, default=20)
        parser.add_argument(
            "--min-remaining",
            type=int,
            default=10,
            help="Requests to leave in the shared UserTweets bucket for live polling.",
        )

    def handle(self, *args, **options):
        account = str(options["account"]).strip().lstrip("@")
        if not account:
            raise CommandError("account is required")

        root = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
        try:
            config_path = _write_config(root)
            # Read the same watermarks the real walk sees, so a probe reflects
            # production. Nothing is written back.
            _restore_state(root, "live")

            env = dict(os.environ)
            env["TDF_PROJECT_ROOT"] = str(root)
            env["TDF_CONFIG"] = str(config_path)
            env["TDF_EMPTY_PAGE_STREAK"] = str(settings.FETCH_EMPTY_PAGE_STREAK)
            env["PYTHONPATH"] = str(settings.BASE_DIR) + os.pathsep + env.get("PYTHONPATH", "")

            process = subprocess.run(
                [
                    sys.executable, "-m", "fetcher.probe",
                    "--account", account,
                    "--pages", str(options["pages"]),
                    "--min-remaining", str(options["min_remaining"]),
                ],
                env=env,
                cwd=str(root),
                capture_output=True,
                text=True,
            )
            literals = _literal_secrets()
            if process.stdout:
                self.stdout.write(redact_text(process.stdout, literals=literals))
            if process.stderr:
                self.stderr.write(redact_text(process.stderr, literals=literals))
            if process.returncode != 0:
                raise CommandError(f"probe exited with code {process.returncode}")
        finally:
            shutil.rmtree(root, ignore_errors=True)
