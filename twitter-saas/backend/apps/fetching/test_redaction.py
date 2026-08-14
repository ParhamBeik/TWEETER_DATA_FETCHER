"""Redaction is a security control and previously had no test at all."""
import pytest

from apps.fetching.redaction import MASK, redact_text
from apps.tweets.models import XSession

LIVE_TOKEN = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


@pytest.mark.django_db
def test_bare_token_on_a_keywordless_line_is_redacted():
    """The old line-based filter missed this: the value and the key are apart.

    A pretty-printed JSON blob or a traceback frame puts the secret on a line
    that mentions none of the keywords, so keyword matching let it through.
    Matching the known literal from XSession catches it.
    """
    XSession.objects.create(name="default", active=True, cookies={"auth_token": LIVE_TOKEN})

    out = redact_text("Traceback line\n" + LIVE_TOKEN + "\nnext line")

    assert LIVE_TOKEN not in out
    assert MASK in out
    assert "Traceback line" in out, "non-secret content must survive"
    assert "next line" in out


def test_key_value_pairs_are_masked_without_a_live_session():
    out = redact_text('{"ct0": "deadbeefdeadbeef", "text": "hello"}', literals=[])
    assert "deadbeefdeadbeef" not in out
    assert "hello" in out, "only the secret value is removed, not the whole payload"


def test_bearer_token_is_masked():
    out = redact_text("authorization: Bearer AAAAAAAAAAAAAAAAAAAAAA%3D%3D", literals=[])
    assert "AAAAAAAAAAAAAAAAAAAAAA" not in out


def test_ordinary_output_is_untouched():
    text = "Page 3 fetched -> 20 tweets for @elonmusk"
    assert redact_text(text, literals=[]) == text


def test_short_values_are_not_blind_replaced():
    """A 2-char cookie value must not blank out unrelated text."""
    out = redact_text("Page 3 fetched", literals=["3"])
    assert out == "Page 3 fetched"
