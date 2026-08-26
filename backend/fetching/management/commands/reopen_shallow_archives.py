"""Re-open archive walks that were marked complete on an ambiguous end signal.

`success_timeline_exhausted` used to set `backfill_complete`, so 45 of 64
accounts stopped at ~45 pages and were reported fully archived -- @elonmusk with
three months of history, @business with thirteen days. The walk now records
*why* it stopped, but rows written before that carry no reason and still read as
complete, so the scheduler will never queue them again.

This clears the flag on those rows so their walks resume against the configured
date floor. Accounts that genuinely reached their first tweet
(`success_true_end`) are left alone.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from tweets.models import EndpointState, TwitterUser

# Outcomes that never justified a completion. Anything else that reached
# `backfill_complete` without a recorded reason is ambiguous by definition:
# the run that set it could not distinguish the two cases.
CONCLUSIVE = "success_true_end"


class Command(BaseCommand):
    help = "Resume archive walks that were completed on X's serving depth."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        tracked = {h.lower() for h in TwitterUser.objects.filter(tracking=True).values_list("handle", flat=True)}

        reopened, kept = [], []
        for state in EndpointState.objects.filter(endpoint="UserTweets"):
            if state.account.lower() not in tracked:
                continue
            data = state.data if isinstance(state.data, dict) else {}
            if not data.get("backfill_complete"):
                continue
            # A reason means the row was written by the current logic, which
            # already tells the truth -- do not churn it.
            if data.get("backfill_depth_reason"):
                kept.append(state.account)
                continue
            if data.get("backfill_last_outcome") == CONCLUSIVE:
                kept.append(state.account)
                continue

            reopened.append(state.account)
            if dry_run:
                continue
            data["backfill_complete"] = False
            data["backfill_depth_reason"] = None
            # Start the resumed walk from the top rather than from the stale
            # cursor: that cursor sits at the depth X refused to serve, so
            # resuming there would immediately re-derive the same dead end.
            data["backfill_cursor"] = None
            data["backfill_pages_done"] = 0
            data["backfill_stalled_ticks"] = 0
            state.data = data
            state.save(update_fields=["data"])

        verb = "would reopen" if dry_run else "reopened"
        self.stdout.write(f"{verb} {len(reopened)} archive walk(s); left {len(kept)} alone")
        for handle in sorted(reopened):
            self.stdout.write(f"  reopen @{handle}")
