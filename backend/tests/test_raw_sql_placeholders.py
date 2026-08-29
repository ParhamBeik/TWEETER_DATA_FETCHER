"""Guard against stray '%' in raw SQL.

The database driver scans an entire query string for placeholders -- SQL
comments included -- before it ever reaches Postgres. A '%' that is not part of
'%s' or an escaped '%%' therefore blows up the request, and it blows up only on
Postgres, which the rest of this suite runs on SQLite and skips. A prose comment
reading "90% of results" took the Narratives panel down in production this way.
Static check, because there is no database that can catch it here.
"""

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
PACKAGES = ("tweets", "fetching", "fetcher", "config", "seed")
SQL_KEYWORD = re.compile(r"\b(SELECT|WITH|INSERT INTO|UPDATE|DELETE FROM)\b")
# Consume the legal forms first so an escaped '%%' cannot be read as one good
# '%' followed by one stray one -- only group 1 is a genuine offender.
STRAY_PERCENT = re.compile(r"%%|%s|%\(|(%)")
TRIPLE_QUOTED = re.compile(r'"""(.*?)"""', re.S)


def _stray(sql):
    return next((m for m in STRAY_PERCENT.finditer(sql) if m.group(1)), None)


def _sql_blocks():
    for package in PACKAGES:
        for path in sorted((BACKEND / package).rglob("*.py")):
            source = path.read_text(encoding="utf-8", errors="replace")
            for match in TRIPLE_QUOTED.finditer(source):
                body = match.group(1)
                if SQL_KEYWORD.search(body):
                    line = source[: match.start()].count("\n") + 1
                    yield path.relative_to(BACKEND), line, body


def test_raw_sql_has_no_unescaped_percent_signs():
    offenders = [
        f"{path}:{line}: {body[max(0, hit.start() - 50):hit.start() + 10]!r}"
        for path, line, body in _sql_blocks()
        for hit in [_stray(body)]
        if hit
    ]
    assert not offenders, "unescaped '%' in raw SQL (use '%%'):\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT 1 WHERE a = %s", False),
        ("SELECT 1 -- 90% of rows", True),
        ("SELECT 1 -- 90%% of rows", False),
        ("SELECT a FROM t WHERE b LIKE 'x%'", True),
    ],
)
def test_the_detector_itself_is_honest(sql, expected):
    assert bool(_stray(sql)) is expected
