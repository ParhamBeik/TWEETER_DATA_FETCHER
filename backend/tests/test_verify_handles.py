"""Classifying a quarantined handle: dead, alive, or the session's fault.

Unit level for the classifier (a pure function over a state blob) plus one
integration case for deletion, since "remove it from both stores" is precisely
the invariant a mocked store would hide.
"""
import pytest

from fetching.management.commands.verify_handles import (
    ALIVE,
    DEAD,
    UNKNOWN,
    classify,
    forget_live_state,
)
from tweets.models import KeyValueState, TwitterUser


def test_a_404_is_the_only_evidence_that_a_handle_is_dead():
    verdict, _ = classify(
        {
            "availability_failure_count": 3,
            "last_availability_evidence": "UserByScreenName failed: UserByScreenName returned HTTP 404",
        }
    )
    assert verdict == DEAD


@pytest.mark.parametrize("status", ["401", "403"])
def test_an_auth_failure_says_nothing_about_the_handle(status):
    """Deleting on 401/403 would drop live accounts every time the session expires."""
    verdict, evidence = classify(
        {
            "availability_failure_count": 3,
            "last_availability_evidence": f"UserByScreenName returned HTTP {status}",
        }
    )
    assert verdict == UNKNOWN
    assert "session" in evidence


def test_a_resolved_handle_is_alive():
    verdict, _ = classify({"user_id": "34713362", "availability_failure_count": 0})
    assert verdict == ALIVE


def test_missing_evidence_is_never_treated_as_dead():
    assert classify({})[0] == UNKNOWN
    assert classify({"availability_failure_count": 3})[0] == UNKNOWN


def test_a_handle_that_resolved_but_later_failed_is_not_alive():
    verdict, _ = classify(
        {"user_id": "1", "availability_failure_count": 2, "last_availability_evidence": "timeout"}
    )
    assert verdict == UNKNOWN


@pytest.mark.django_db
def test_deleting_clears_the_engine_state_too_so_a_reseed_cannot_resurrect_it():
    TwitterUser.objects.create(handle="BBCVerify", tracking=True, quarantined=True)
    KeyValueState.objects.create(
        namespace="request_state",
        name="historical_live:live_state.json",
        data={"bbcverify": {"quarantined": True}, "business": {"user_id": "1"}},
    )

    forget_live_state("BBCVerify")

    row = KeyValueState.objects.get(name="historical_live:live_state.json")
    assert "bbcverify" not in row.data
    assert "business" in row.data
