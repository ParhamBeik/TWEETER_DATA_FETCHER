"""Readable text derived from what X actually served.

`Tweet.text` is deliberately verbatim: this is an archive, exports are supposed
to match the source, and once the original wording is normalised away it cannot
be recovered. But two artefacts of X's own encoding make that raw string a poor
thing to read or search:

- entities are HTML-escaped, so "R&D" arrives as "R&amp;D" and renders literally;
- a link is frequently repeated, because X appends the card's t.co to `full_text`
  even when the author already typed it. 14% of the archive carries the same
  shortlink twice in a row.

Both are upstream, not ingestion bugs -- the raw payload contains them. So the
fix is a second, derived column rather than a rewrite of the first.

Deliberately pure and dependency-free, so the same function serves ingestion, the
backfill migration, and the tests, and so it can be checked without a database.
"""
from __future__ import annotations

import html
import re

# t.co is the only shortener X emits, and matching it specifically means a
# legitimately repeated ordinary word or URL in someone's prose is left alone.
_TCO = re.compile(r"https://t\.co/[A-Za-z0-9]+")


def clean_text(raw: str | None) -> str:
    """Decode X's HTML escaping and drop repeated t.co links.

    Only *repeats* are removed, and only the second and later occurrences, so the
    first mention keeps its position in the sentence and a post that legitimately
    links two different things is untouched.
    """
    if not raw:
        return ""
    text = html.unescape(str(raw))

    seen: set[str] = set()

    def keep(match: re.Match) -> str:
        url = match.group(0)
        if url in seen:
            return ""
        seen.add(url)
        return url

    text = _TCO.sub(keep, text)
    # Removing a link mid-string leaves the whitespace that surrounded it.
    # Collapse runs of spaces/tabs but never newlines: paragraph breaks are
    # meaningful in the long-form posts these accounts write.
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def _self_check() -> None:
    """The four behaviours that make this safe to run over the whole archive."""
    # HTML entities are decoded.
    assert clean_text("this &amp; that") == "this & that"
    assert clean_text("&lt;tag&gt; &quot;q&quot;") == '<tag> "q"'

    # A repeated shortlink collapses to one, keeping the first position.
    assert (
        clean_text("Ecuador case https://t.co/Mb2Vix3XUC https://t.co/Mb2Vix3XUC")
        == "Ecuador case https://t.co/Mb2Vix3XUC"
    )

    # Two *different* links both survive -- this is the case that must not break.
    both = "see https://t.co/aaaa and https://t.co/bbbb"
    assert clean_text(both) == both, clean_text(both)

    # A repeat that is not adjacent still collapses, and the prose survives.
    assert (
        clean_text("https://t.co/aaaa start middle end https://t.co/aaaa")
        == "https://t.co/aaaa start middle end"
    )

    # Newlines are structure, not whitespace noise.
    assert clean_text("line one\n\nline two") == "line one\n\nline two"

    # Empty and None are safe, because ingestion calls this on every row.
    assert clean_text(None) == ""
    assert clean_text("") == ""

    # Idempotent: the backfill can be re-run without degrading a cleaned row.
    once = clean_text("a &amp; b https://t.co/zz https://t.co/zz")
    assert clean_text(once) == once, once

    print("tweets.textclean self-check passed")


if __name__ == "__main__":  # pragma: no cover - manual check
    _self_check()
