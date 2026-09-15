"""The engine's UTC clock, in one place.

``datetime.utcnow()`` is deprecated and scheduled for removal: it returns a naive
datetime that merely *claims* to be UTC, which is the confusion the deprecation
exists to end. Running the suite on Python 3.13 reports it 38 times and nothing
else, so it is the only thing standing between this project and a newer runtime.

The replacement has to reproduce the old value exactly rather than be correct in
the abstract. Every timestamp this engine has ever written -- endpoint state
blobs, run reports, ``events.jsonl`` -- is a naive-UTC ISO string with a literal
``Z`` appended, and the scheduling checks subtract one parsed back out of "now".
An aware datetime there would raise ``TypeError`` on the subtraction, and an
aware ``isoformat()`` would start writing ``+00:00`` into files that already hold
``Z``. Hence naive, and hence the string helper next to it.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Now in UTC, naive -- the value ``datetime.utcnow()`` used to return."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_now_iso() -> str:
    """Now as the ``...Z`` ISO string every stored timestamp here is written in."""
    return utc_now().isoformat() + "Z"
