"""Stamp honest depth reasons; reopen only contradictory completions.

`success_timeline_exhausted` used to set `backfill_complete` with no reason, so
Pulse counted those accounts as fully archived. Retrying UserTweets cannot pass
X's serving depth, so this no longer clears the flag on exhausted rows -- it
writes `provider_depth_limit` instead. Rows completed on `paused_for_quota`
(or anything else non-terminal) are still reopened so the walk can continue.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from fetching.accounts import PROVIDER_DEPTH_LIMIT
from tweets.models import EndpointState, TwitterUser

TRUE_END = "success_true_end"
EXHAUSTED = "success_timeline_exhausted"


class Command(BaseCommand):
    help = "Stamp provider-depth stops honestly; reopen contradictory completions."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        tracked = {
            h.lower()
            for h in TwitterUser.objects.filter(tracking=True).values_list("handle", flat=True)
        }

        stamped, reopened, kept = [], [], []
        for state in EndpointState.objects.filter(endpoint="UserTweets"):
            if state.account.lower() not in tracked:
                continue
            data = state.data if isinstance(state.data, dict) else {}
            if not data.get("backfill_complete"):
                continue
            reason = data.get("backfill_depth_reason")
            outcome = data.get("backfill_last_outcome")
            if reason:
                kept.append(state.account)
                continue
            if outcome == TRUE_END:
                kept.append(state.account)
                continue
            if outcome == EXHAUSTED:
                stamped.append(state.account)
                if dry_run:
                    continue
                data["backfill_depth_reason"] = PROVIDER_DEPTH_LIMIT
                state.data = data
                state.save(update_fields=["data"])
                continue

            reopened.append(state.account)
            if dry_run:
                continue
            data["backfill_complete"] = False
            data["backfill_depth_reason"] = None
            data["backfill_cursor"] = None
            data["backfill_pages_done"] = 0
            data["backfill_stalled_ticks"] = 0
            state.data = data
            state.save(update_fields=["data"])

        verb_stamp = "would stamp" if dry_run else "stamped"
        verb_reopen = "would reopen" if dry_run else "reopened"
        self.stdout.write(
            f"{verb_stamp} {len(stamped)} provider-depth stop(s); "
            f"{verb_reopen} {len(reopened)}; left {len(kept)} alone"
        )
        for handle in sorted(stamped):
            self.stdout.write(f"  stamp @{handle}")
        for handle in sorted(reopened):
            self.stdout.write(f"  reopen @{handle}")
