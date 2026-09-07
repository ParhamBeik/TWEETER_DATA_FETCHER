"""Void the archive verdicts produced by the dropped-conversation bug.

`success_timeline_exhausted` means consecutive pages held no tweets. Until
`TweetSetProcessor._entry_tweets` was fixed, a page whose posts were all inside
self-thread modules extracted nothing, so that signal fired on accounts whose
timelines were still being served normally -- @geoconfirmed was parked after two
pages holding 15 of its posts, and a probe of the same eight pages afterwards
returned 140. Every verdict reached through that signal is evidence about a bug,
not about X, and none of them can be trusted.

Reopening is scoped to exactly that signal. `reached_date_floor` (we chose to
stop) and `reached_first_tweet` (X ran out of cursor) are untouched. A later
honest provider-depth stop keeps a wall cursor so a monthly probe can resume;
the broken walks stored none. A row that still has a usable cursor is therefore
left alone -- re-running this command after the extractor fix must not restart
those archives from the top.

Walk state lives in two Postgres rows: `EndpointState` (what the queue reads)
and `KeyValueState` `sync_state/historical_live` (what the engine restores).
Persist copies the engine blob onto every EndpointState row, so clearing only
the queue copy is undone by the next tick. Each copy is voided only if *that*
copy still carries the false-stop verdict. An honest completion in either store
is copied onto the other so persist cannot resurrect the parked copy.

    manage.py reopen_false_depth_stops --dry-run
"""
from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fetching.accounts import PROVIDER_DEPTH_LIMIT
from fetching.tasks import reap_orphaned_fetch_runs
from tweets.models import EndpointState, FetchRun, KeyValueState, TwitterUser

EXHAUSTED = "success_timeline_exhausted"
SYNC_NAMESPACE = "sync_state"
SYNC_NAME = "historical_live"
_CYCLE_LOCKS = ("backfill_historical_all", "poll_live_all")
_ACCOUNT_LOCKS = ("fetch_account_historical", "fetch_account_live")
_VERDICT_KEYS = (
    "backfill_complete",
    "backfill_depth_reason",
    "backfill_completed_at",
    "backfill_cursor",
    "backfill_last_outcome",
    "backfill_pages_done",
    "backfill_stalled_ticks",
    "backfill_empty_streak",
    "backfill_floor_date",
)


def _usable_cursor(data: dict) -> bool:
    cursor = data.get("backfill_cursor")
    return bool(cursor) and str(cursor) not in {"__START__", "__END__"}


def is_false_depth_stop(data: dict) -> bool:
    """True when completeness was inferred from the broken empty-page signal.

    Post-fix walks that really hit X's wall keep a cursor. The corrupted rows
    this command exists to repair stored none.
    """
    if not data.get("backfill_complete"):
        return False
    if _usable_cursor(data):
        return False
    reason = data.get("backfill_depth_reason")
    outcome = data.get("backfill_last_outcome")
    return outcome == EXHAUSTED and reason in (None, "", PROVIDER_DEPTH_LIMIT)


def is_honest_completion(data: dict) -> bool:
    return bool(data.get("backfill_complete")) and not is_false_depth_stop(data)


def void_false_depth_stop(data: dict) -> dict:
    """Clear the parked-at-the-wall verdict so the walk starts from the top."""
    updated = dict(data)
    updated.update({
        "backfill_complete": False,
        "backfill_depth_reason": None,
        "backfill_completed_at": None,
        "backfill_cursor": None,
        "backfill_pages_done": 0,
        "backfill_stalled_ticks": 0,
        "backfill_last_outcome": None,
        "backfill_empty_streak": 0,
    })
    return updated


def copy_honest_verdict(source: dict, dest: dict) -> dict:
    updated = dict(dest)
    for key in _VERDICT_KEYS:
        updated[key] = source.get(key)
    return updated


def _keep_reason(data: dict) -> str:
    return str(data.get("backfill_depth_reason") or data.get("backfill_last_outcome") or "unknown")


def _user_tweets(account_state: object) -> dict:
    if not isinstance(account_state, dict):
        return {}
    user_tweets = account_state.get("UserTweets")
    return user_tweets if isinstance(user_tweets, dict) else {}


def _lock_names(handles: list[str]) -> list[str]:
    names = list(_CYCLE_LOCKS)
    for handle in handles:
        for kind in _ACCOUNT_LOCKS:
            names.append(f"{kind}:{handle}")
    return names


def _acquire_write_locks(handles: list[str]) -> list[str] | None:
    """Hold every lock a live/historical persist can be running under.

    Fleet ticks take the cycle names. Staff Fetch takes per-account names and
    never the cycle names, so checking those keys after the fact is not enough:
    a fetch that starts between the check and the write restores the parked
    blob and persist puts it back.
    """
    timeout = settings.FETCH_CYCLE_TIMEOUT_SECONDS + 60
    held: list[str] = []
    for name in _lock_names(handles):
        key = f"fetch_cycle_lock:{name}"
        if not cache.add(key, "1", timeout=timeout):
            for held_key in held:
                cache.delete(held_key)
            return None
        held.append(key)
    return held


def _running_fetch() -> bool:
    return FetchRun.objects.filter(status="running", subsystem__in=("live", "historical")).exists()


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
        if dry_run:
            endpoints, blob, sync_keys = self._load()
            reopened, skipped = self._classify(
                endpoints, blob, sync_keys, tracked, all_accounts=options["all_accounts"]
            )
            self._report(reopened, skipped, dry_run=True)
            return

        handles = list(
            TwitterUser.objects.filter(tracking=True).values_list("handle", flat=True)
        )
        reap_orphaned_fetch_runs()
        held = _acquire_write_locks(handles)
        if held is None:
            raise CommandError(
                "a live or historical fetch is running; retry when both workers are idle"
            )
        try:
            if _running_fetch():
                raise CommandError(
                    "a live or historical fetch is running; retry when both workers are idle"
                )
            with transaction.atomic():
                endpoints, blob, sync_keys, sync = self._load_locked()
                reopened, skipped = self._classify(
                    endpoints, blob, sync_keys, tracked, all_accounts=options["all_accounts"]
                )
                blob_changed = False
                for key, display in list(reopened.items()):
                    ep = endpoints.get(key)
                    if ep is not None:
                        ep_data = ep.data if isinstance(ep.data, dict) else {}
                        if is_false_depth_stop(ep_data):
                            ep.data = void_false_depth_stop(ep_data)
                            ep.save(update_fields=["data", "updated_at"])
                    account_key = sync_keys.get(key)
                    if account_key is None:
                        continue
                    account_state = blob.get(account_key)
                    user_tweets = _user_tweets(account_state)
                    if is_false_depth_stop(user_tweets):
                        blob[account_key] = {
                            **account_state,
                            "UserTweets": void_false_depth_stop(user_tweets),
                        }
                        blob_changed = True
                for key, (display, _why) in skipped.items():
                    ep = endpoints.get(key)
                    ep_data = ep.data if ep is not None and isinstance(ep.data, dict) else {}
                    account_key = sync_keys.get(key)
                    kv_data = _user_tweets(blob.get(account_key) if account_key else None)
                    if is_honest_completion(ep_data) and is_false_depth_stop(kv_data) and account_key:
                        blob[account_key] = {
                            **blob[account_key],
                            "UserTweets": copy_honest_verdict(ep_data, kv_data),
                        }
                        blob_changed = True
                    elif is_honest_completion(kv_data) and is_false_depth_stop(ep_data) and ep is not None:
                        ep.data = copy_honest_verdict(kv_data, ep_data)
                        ep.save(update_fields=["data", "updated_at"])
                if sync is not None and blob_changed:
                    sync.data = blob
                    sync.save(update_fields=["data", "updated_at"])
        finally:
            for key in held:
                cache.delete(key)

        self._report(reopened, skipped, dry_run=False)

    def _load(self) -> tuple[dict[str, EndpointState], dict, dict[str, str]]:
        endpoints: dict[str, EndpointState] = {}
        for state in EndpointState.objects.filter(endpoint="UserTweets"):
            endpoints[state.account.lower()] = state
        sync = KeyValueState.objects.filter(namespace=SYNC_NAMESPACE, name=SYNC_NAME).first()
        blob = dict(sync.data) if sync and isinstance(sync.data, dict) else {}
        sync_keys = {str(account).lower(): str(account) for account in blob}
        return endpoints, blob, sync_keys

    def _load_locked(self) -> tuple[dict[str, EndpointState], dict, dict[str, str], KeyValueState | None]:
        endpoints: dict[str, EndpointState] = {}
        for state in EndpointState.objects.select_for_update().filter(endpoint="UserTweets"):
            endpoints[state.account.lower()] = state
        sync = (
            KeyValueState.objects.select_for_update()
            .filter(namespace=SYNC_NAMESPACE, name=SYNC_NAME)
            .first()
        )
        blob = dict(sync.data) if sync and isinstance(sync.data, dict) else {}
        sync_keys = {str(account).lower(): str(account) for account in blob}
        return endpoints, blob, sync_keys, sync

    def _classify(
        self,
        endpoints: dict[str, EndpointState],
        blob: dict,
        sync_keys: dict[str, str],
        tracked: set[str],
        *,
        all_accounts: bool,
    ) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
        reopened: dict[str, str] = {}
        skipped: dict[str, tuple[str, str]] = {}
        for key in sorted(set(endpoints) | set(sync_keys)):
            if not all_accounts and key not in tracked:
                continue
            ep = endpoints.get(key)
            ep_data = ep.data if ep is not None and isinstance(ep.data, dict) else {}
            account_key = sync_keys.get(key)
            kv_data = _user_tweets(blob.get(account_key) if account_key else None)
            display = ep.account if ep is not None else (account_key or key)
            if is_honest_completion(ep_data) or is_honest_completion(kv_data):
                skipped[key] = (
                    display,
                    _keep_reason(ep_data if is_honest_completion(ep_data) else kv_data),
                )
                continue
            if is_false_depth_stop(ep_data) or is_false_depth_stop(kv_data):
                reopened[key] = display
                continue
            if ep_data.get("backfill_complete") or kv_data.get("backfill_complete"):
                skipped[key] = (display, _keep_reason(ep_data or kv_data))
        return reopened, skipped

    def _report(self, reopened: dict[str, str], skipped: dict[str, tuple[str, str]], *, dry_run: bool) -> None:
        verb = "would reopen" if dry_run else "reopened"
        self.stdout.write(f"{verb} {len(reopened)} archive walk(s); left {len(skipped)} alone")
        for handle in sorted(reopened.values(), key=str.lower):
            self.stdout.write(f"  reopen @{handle}")
        for handle, why in sorted(skipped.values(), key=lambda item: item[0].lower()):
            self.stdout.write(f"  keep   @{handle} ({why})")
