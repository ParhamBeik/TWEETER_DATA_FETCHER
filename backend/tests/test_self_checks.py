"""Run the modules' own `_self_check()` blocks as part of the suite.

Three pure modules ship a `_self_check()` behind `if __name__ == "__main__"`:
`tweets.topics`, `tweets.textclean` and `fetching.media`. Between them they hold
34 assertions about the rules their authors called load-bearing -- and nothing
ran them. Not pytest, not CI, not the deploy. An assertion that never executes is
worse than no assertion: it reads like a guarantee and is not one.

They are not duplicates of the existing tests. `tests/test_feed_fixes.py` checks
one `clean_text` case through the API; the self-check is the one that pins *two
different links both survive*, that a repeat which is not adjacent still
collapses, that newlines are structure rather than whitespace, and that the
function is idempotent -- which is what makes the backfill migration safe to
re-run. Same shape for the other two: the video-variant choice must be
deterministic or files are stored under one URL and looked up under another, and
the topic ranking's containment and filler rules are the difference between a
useful panel and noise.

So this file does not restate them. It executes them where they will be seen.
"""
from __future__ import annotations

import pytest

from fetching import media
from tweets import textclean, topics


@pytest.mark.parametrize(
    "module",
    [topics, textclean, media],
    ids=lambda module: module.__name__,
)
def test_module_self_check_passes(module, capsys):
    """The module's own assertions, executed.

    A failure here points at the module's `_self_check()`, which names the rule
    it was defending in the line right above the assertion.
    """
    module._self_check()
    # Each one prints on success; asserting that keeps this from passing
    # vacuously if a `_self_check` is ever emptied out into a no-op.
    assert "self-check passed" in capsys.readouterr().out
