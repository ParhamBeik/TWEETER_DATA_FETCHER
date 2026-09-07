"""Void the archive verdicts produced by the dropped-conversation bug.

`success_timeline_exhausted` means "two consecutive pages held no tweets". Until
`TweetSetProcessor._entry_tweets` was fixed, a page whose posts were all inside
self-thread modules extracted nothing, so that signal fired on accounts whose
timelines were still being served normally -- @geoconfirmed was parked after two
pages holding 15 of its posts, and a probe of the same eight pages afterwards
returned 140. Every verdict reached through that signal is evidence about a bug,
not about X, and none of them can be trusted.

Reopening is scoped to exactly that signal. `reached_date_floor` (we chose to
stop) and `reached_first_tweet` (X ran out of cursor) are untouched: neither
depends on counting tweets per page, so neither is affected.

This is a one-shot repair for archives walked before the fix. It is safe to
re-run -- a walk that has since finished honestly records a different reason and
is skipped -- but it should not need to run twice.

    manage.py reopen_false_depth_stops --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from fetching.accounts import PROVIDER_DEPTH_LIMIT
from tweets.models import EndpointState, TwitterUser

EXHAUSTED = "success_timeline_exhausted"


class Command(BaseCommand):
    help = "Reopen archive walks stopped by the dropped-conversation extraction bug."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--all-accounts",
            action="store_true",
            help="Include rows for accounts no longer tracked.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        tracked = {
            handle.lower()
            for handle in TwitterUser.objects.filter(tracking=True).values_list("handle", flat=True)
        }

        reopened: list[str] = []
        skipped: list[tuple[str, str]] = []
        for state in EndpointState.objects.filter(endpoint="UserTweets"):
            handle = state.account.lower()
            if not options["all_accounts"] and handle not in tracked:
                continue
            data = state.data if isinstance(state.data, dict) else {}
            if not data.get("backfill_complete"):
                continue

            reason = data.get("backfill_depth_reason")
            outcome = data.get("backfill_last_outcome")
            # Rows written before backfill_depth_reason existed carry the outcome
            # only -- the same pairing fetching.accounts._is_provider_depth reads.
            corrupted = outcome == EXHAUSTED and reason in (None, "", PROVIDER_DEPTH_LIMIT)
            if not corrupted:
                skipped.append((state.account, str(reason or outcome or "unknown")))
                continue

            reopened.append(state.account)
            if dry_run:
                continue
            # Clear the cursor too. It points at the page the broken walk gave up
            # on, and resuming there would skip everything above it that the
            # extractor never saw -- which is the whole point of reopening.
            data.update({
                "backfill_complete": False,
                "backfill_depth_reason": None,
                "backfill_completed_at": None,
                "backfill_cursor": None,
                "backfill_pages_done": 0,
                "backfill_stalled_ticks": 0,
            })
            state.data = data
            state.save(update_fields=["data"])

        verb = "would reopen" if dry_run else "reopened"
        self.stdout.write(f"{verb} {len(reopened)} archive walk(s); left {len(skipped)} alone")
        for handle in sorted(reopened):
            self.stdout.write(f"  reopen @{handle}")
        for handle, why in sorted(skipped):
            self.stdout.write(f"  keep   @{handle} ({why})")
