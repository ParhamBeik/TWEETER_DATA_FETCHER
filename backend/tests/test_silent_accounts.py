"""An account can be polled perfectly and still be collecting nothing.

Every 30-minute poll of @realdonaldtrump reported `completed` for 41 days. It
was not lying about the fetch -- X really did return 200 with a page of tweets --
but completeness only ever asked whether pagination had reached *back* past the
window start, which a page of stale posts satisfies on page one. Nothing asked
whether anything new had arrived, so the console showed green for six weeks.

These pin the signal that answers that question, including the part that makes
it usable: silence is measured against each account's own cadence, so a tier-7
account that posts fortnightly does not sit permanently in the same list as a
newswire that has stopped mid-morning.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from fetching.accounts import SILENCE_GAP_MULTIPLE, SILENCE_MIN_HOURS, silent_accounts
from fetching.ingest import upsert_tweet
from tweets.models import KeyValueState, TwitterUser

HOUR = 3600


def _polled(handle, checked_at=None, status="completed"):
    """Record that the live poller believes it is handling this account."""
    row, _ = KeyValueState.objects.get_or_create(
        namespace="request_state",
        name="historical_live:live_state.json",
        defaults={"data": {}},
    )
    data = dict(row.data or {})
    data[handle.lower()] = {
        "last_checked_at": (checked_at or timezone.now()).isoformat(),
        "last_status": status,
    }
    row.data = data
    row.save()


def _posted(handle, when):
    upsert_tweet({
        "id": f"{handle}-{when.timestamp()}",
        "account": handle,
        "text": "x",
        "created_at": when.isoformat(),
    })


@pytest.mark.django_db
def test_an_account_frozen_for_weeks_is_reported_despite_healthy_polls():
    TwitterUser.objects.create(
        handle="realdonaldtrump", tracking=True, priority=1,
        observed_median_gap_seconds=HOUR,
    )
    _posted("realdonaldtrump", timezone.now() - timedelta(days=41))
    _polled("realdonaldtrump")

    silent = silent_accounts()

    assert [row["handle"] for row in silent] == ["realdonaldtrump"]
    assert silent[0]["last_status"] == "completed"
    assert silent[0]["quiet_hours"] > 24 * 40


@pytest.mark.django_db
def test_a_currently_posting_account_is_not_reported():
    TwitterUser.objects.create(
        handle="reuters", tracking=True, priority=2, observed_median_gap_seconds=HOUR,
    )
    _posted("reuters", timezone.now() - timedelta(minutes=10))
    _polled("reuters")

    assert silent_accounts() == []


@pytest.mark.django_db
def test_silence_is_judged_against_the_account_s_own_cadence():
    """Two days quiet: alarming for a newswire, routine for a fortnightly poster."""
    now = timezone.now()
    TwitterUser.objects.create(
        handle="reuters", tracking=True, priority=2, observed_median_gap_seconds=HOUR,
    )
    TwitterUser.objects.create(
        handle="wgc_news", tracking=True, priority=7,
        observed_median_gap_seconds=14 * 24 * HOUR,
    )
    for handle in ("reuters", "wgc_news"):
        _posted(handle, now - timedelta(days=2))
        _polled(handle)

    assert [row["handle"] for row in silent_accounts(now=now)] == ["reuters"]


@pytest.mark.django_db
def test_a_fast_account_still_gets_a_minimum_grace_period():
    """A minute-by-minute account must not be flagged over a quiet lunch hour."""
    now = timezone.now()
    TwitterUser.objects.create(
        handle="business", tracking=True, priority=2, observed_median_gap_seconds=60,
    )
    _posted("business", now - timedelta(hours=SILENCE_MIN_HOURS - 1))
    _polled("business")

    assert silent_accounts(now=now) == []
    assert 60 * SILENCE_GAP_MULTIPLE < SILENCE_MIN_HOURS * HOUR


@pytest.mark.django_db
def test_an_account_with_no_tweets_at_all_leads_the_list():
    TwitterUser.objects.create(handle="amosharel", tracking=True, priority=5)
    TwitterUser.objects.create(
        handle="lagarde", tracking=True, priority=2, observed_median_gap_seconds=HOUR,
    )
    _posted("lagarde", timezone.now() - timedelta(days=19))
    _polled("amosharel")
    _polled("lagarde")

    silent = silent_accounts()

    assert [row["handle"] for row in silent] == ["amosharel", "lagarde"]
    assert silent[0]["quiet_hours"] is None


@pytest.mark.django_db
def test_an_account_the_poller_has_never_touched_is_a_different_problem():
    """Never-polled accounts would drown the signal this list exists to carry."""
    TwitterUser.objects.create(handle="newlyadded", tracking=True, priority=5)

    assert silent_accounts() == []


@pytest.mark.django_db
def test_quarantined_accounts_are_already_reported_elsewhere():
    TwitterUser.objects.create(
        handle="blocked", tracking=True, priority=2, quarantined=True,
        observed_median_gap_seconds=HOUR,
    )
    _posted("blocked", timezone.now() - timedelta(days=30))
    _polled("blocked")

    assert silent_accounts() == []
