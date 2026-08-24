"""Decide whether a quarantined account is actually dead, and optionally drop it.

An account quarantines after three consecutive user-ID resolution failures, but
the quarantine itself does not say *why*: a handle that never existed, a handle
that was renamed, and an expired X session all look identical from the outside.
The engine already records the distinguishing evidence (the HTTP status
UserByScreenName returned), so this reads that rather than guessing -- and
``--recheck`` refreshes it by resolving the handles once more.

    python manage.py verify_handles                     # report on all quarantined
    python manage.py verify_handles --recheck           # resolve them again first
    python manage.py verify_handles --recheck --apply   # and delete the true 404s
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand

from fetching import runner
from fetching.accounts import clear_live_quarantine, live_state_map
from tweets.models import KeyValueState, TwitterUser

LIVE_MODULE = "fetcher.live"

# "UserByScreenName returned HTTP 404" -- the handle does not resolve to an
# account. 401/403 mean the operator session expired and says nothing about the
# handle, so those must never be treated as dead.
_STATUS = re.compile(r"HTTP (\d{3})")

DEAD = "dead"
ALIVE = "alive"
UNKNOWN = "unknown"


def classify(state: dict) -> tuple[str, str]:
    """(verdict, evidence) for one account's live state blob."""
    evidence = str(state.get("last_availability_evidence") or "").strip()
    if state.get("user_id") and not int(state.get("availability_failure_count", 0) or 0):
        return ALIVE, evidence or "resolved to a user id"
    match = _STATUS.search(evidence)
    if match and match.group(1) == "404":
        return DEAD, evidence
    if match and match.group(1) in {"401", "403"}:
        return UNKNOWN, f"{evidence} (operator session problem, not the handle)"
    return UNKNOWN, evidence or "no recorded evidence"


def forget_live_state(handle: str) -> None:
    """Drop an account's key from the live-state blob.

    Deleting only the TwitterUser row leaves the engine's own copy behind, and a
    re-seed would reinstate a quarantined account that never gets re-examined.
    Both stores or neither.
    """
    key = handle.lower().lstrip("@")
    for name in ("historical_live:live_state.json", "live_state.json"):
        row = KeyValueState.objects.filter(namespace="request_state", name=name).first()
        if row is None or not isinstance(row.data, dict) or key not in row.data:
            continue
        data = dict(row.data)
        data.pop(key, None)
        row.data = data
        row.save(update_fields=["data", "updated_at"])


class Command(BaseCommand):
    help = "Report which quarantined accounts are genuinely dead; optionally delete them."

    def add_arguments(self, parser):
        parser.add_argument(
            "handles", nargs="*", help="Handles to check. Default: every quarantined account."
        )
        parser.add_argument(
            "--recheck",
            action="store_true",
            help="Clear quarantine and resolve the handles once more before judging.",
        )
        parser.add_argument(
            "--apply", action="store_true", help="Delete the handles that resolve as dead."
        )

    def handle(self, *args, **options):
        handles = [h.lstrip("@") for h in options["handles"]] or list(
            TwitterUser.objects.filter(quarantined=True).values_list("handle", flat=True)
        )
        if not handles:
            self.stdout.write("No quarantined accounts.")
            return

        if options["recheck"]:
            # run_cycle skips quarantined accounts before it ever reaches
            # resolution, so the quarantine has to come off for the retry to
            # produce new evidence. It goes straight back on if it fails again.
            for handle in handles:
                clear_live_quarantine(handle)
                TwitterUser.objects.filter(handle__iexact=handle).update(
                    quarantined=False, quarantine_reason="", quarantined_at=None
                )
            args_list = ["--once"]
            for handle in handles:
                args_list += ["--account", handle]
            self.stdout.write(f"Resolving {len(handles)} handle(s)...")
            result = runner.run_fetcher(LIVE_MODULE, args_list, "live", target="verify_handles")
            runner.cleanup(result.root)

        live = live_state_map()
        verdicts: dict[str, tuple[str, str]] = {}
        for handle in handles:
            verdict, evidence = classify(live.get(handle.lower(), {}))
            verdicts[handle] = (verdict, evidence)
            self.stdout.write(f"  @{handle:<20} {verdict:<8} {evidence[:110]}")

        dead = [h for h, (verdict, _) in verdicts.items() if verdict == DEAD]
        unknown = [h for h, (verdict, _) in verdicts.items() if verdict == UNKNOWN]

        if not options["apply"]:
            self.stdout.write(
                f"\n{len(dead)} dead, {len(unknown)} inconclusive. Re-run with --apply to delete the dead."
            )
            return

        for handle in dead:
            forget_live_state(handle)
            TwitterUser.objects.filter(handle__iexact=handle).delete()
            self.stdout.write(self.style.WARNING(f"deleted @{handle}"))
        if unknown:
            self.stdout.write(
                self.style.NOTICE(
                    f"kept {len(unknown)} inconclusive handle(s): {', '.join('@' + h for h in unknown)}"
                )
            )
