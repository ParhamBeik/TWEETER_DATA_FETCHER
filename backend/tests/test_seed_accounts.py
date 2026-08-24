"""Confirmed-dead handles must not return via seed_data.

Unit-level over the seed JSON: if these four names are in the file, the next
`manage.py seed_data` recreates accounts we already deleted on the VPS.
"""
import json
from pathlib import Path

from django.conf import settings

DEAD = {"bbcverify", "bentallblu", "hollydagres", "robinbrooksiif"}


def test_seed_accounts_do_not_include_confirmed_dead_handles():
    data = json.loads((Path(settings.BASE_DIR) / "seed" / "accounts.json").read_text())
    handles = {
        str(entry.get("username") or "").lstrip("@").lower()
        for entries in data.values()
        if isinstance(entries, list)
        for entry in entries
    }
    assert not (DEAD & handles)
