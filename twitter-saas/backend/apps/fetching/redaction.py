"""Secret redaction for fetcher output.

Applied before persisting to Postgres, not on the way out through the API: the
raw text otherwise sits in tweets_fetchrun.log_excerpt where pg_dump, the backup
script, and the Django admin change form all read it unredacted.

Three passes, cheapest and most precise first:

1. Exact values from the active XSession. Keyword matching cannot catch a bare
   token printed on a line that mentions no keyword (a traceback frame, or a
   pretty-printed JSON blob where the key and value land on separate lines).
   Matching the literal secret does, with no false positives.
2. ``key: value`` / ``key=value`` pairs for known secret-ish names.
3. Whole-line fallback for any line still mentioning a secret name.
"""
from __future__ import annotations

import re

MASK = "[redacted]"

_SECRET_NAMES = (
    "auth_token", "authorization", "bearer", "cookie", "csrf", "ct0",
    "guest_id", "kdt", "twid", "x-csrf-token", "x-client-transaction-id",
)

_KV = re.compile(
    r'(?i)("?\b(?:' + "|".join(re.escape(n) for n in _SECRET_NAMES) + r')\b"?\s*[:=]\s*)'
    r'("?)([^\s",;})\]]+)',
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/%]+=*")

# Below this length a "secret" is not distinctive enough to blind-replace; doing
# so would mangle unrelated output.
_MIN_LITERAL_LEN = 8


def _literal_secrets() -> list[str]:
    """Current live secret values, longest first so substrings do not shadow."""
    try:
        from apps.tweets.models import XSession
    except Exception:  # pragma: no cover - app registry not ready
        return []
    try:
        session = XSession.objects.filter(active=True).first()
    except Exception:  # pragma: no cover - DB unavailable
        return []
    if session is None:
        return []
    values: set[str] = set()
    for blob in (session.cookies, session.headers):
        if not isinstance(blob, dict):
            continue
        for value in blob.values():
            text = str(value or "")
            if len(text) >= _MIN_LITERAL_LEN:
                values.add(text)
                # "Bearer abc..." also leaks as the bare token.
                if " " in text:
                    tail = text.rsplit(" ", 1)[-1]
                    if len(tail) >= _MIN_LITERAL_LEN:
                        values.add(tail)
    return sorted(values, key=len, reverse=True)


def redact_text(value: str, *, literals: list[str] | None = None) -> str:
    text = str(value or "")
    if not text:
        return text
    # Enforce the length floor here rather than only where literals are collected,
    # so a caller passing its own list cannot blind-replace a 1-char "secret"
    # across unrelated output.
    for secret in (_literal_secrets() if literals is None else literals):
        if secret and len(secret) >= _MIN_LITERAL_LEN:
            text = text.replace(secret, MASK)
    text = _BEARER.sub(f"Bearer {MASK}", text)
    text = _KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    out = []
    for line in text.splitlines():
        lower = line.lower()
        if MASK not in line and any(name in lower for name in _SECRET_NAMES):
            out.append(MASK)
        else:
            out.append(line)
    return "\n".join(out)
