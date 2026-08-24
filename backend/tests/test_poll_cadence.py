"""Polling cadence: measured rate, clamped by tier.

Unit level for the two pure functions (a median and a clamp -- no DB, no
network), and one integration case for the task that writes them, since the
point of that task is the round-trip into TwitterUser rows.

The behaviour being pinned: a fixed per-tier interval spends the same quota on
an account that posts forty times a day and one that posts twice a week, and
the shared X budget is only 4,800 requests a day for 68 accounts.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from fetching.accounts import MIN_SAMPLE_TWEETS, interval_for, median_gap_seconds, policy_for
from fetching.ingest import upsert_tweet
from fetching.tasks import recompute_poll_intervals
from tweets.models import TwitterUser

HOUR = 3600


def _times(count, gap_seconds, start=None):
    base = start or timezone.now() - timedelta(days=5)
    return [base + timedelta(seconds=index * gap_seconds) for index in range(count)]


def test_median_gap_is_the_typical_gap_not_the_average_one():
    """One overnight silence must not drag the whole estimate with it."""
    times = _times(10, HOUR)
    times.append(times[-1] + timedelta(hours=48))

    assert median_gap_seconds(times) == HOUR


def test_too_few_tweets_yields_no_measurement():
    assert median_gap_seconds(_times(MIN_SAMPLE_TWEETS - 1, HOUR)) is None
    assert median_gap_seconds([]) is None


def test_unordered_timestamps_still_measure_correctly():
    times = _times(8, HOUR)
    assert median_gap_seconds(list(reversed(times))) == HOUR


def test_a_quiet_important_account_is_still_polled_at_its_tier_floor():
    """Priority 1 posting once a day stays on the tier's slowest allowed cadence."""
    interval = interval_for(1, gap_seconds=24 * HOUR)

    assert interval == policy_for(1)["poll_interval_max_seconds"]
    assert interval == HOUR


def test_a_chatty_unimportant_account_cannot_outpace_a_priority_one():
    fast_low_tier = interval_for(7, gap_seconds=60)
    slow_high_tier = interval_for(1, gap_seconds=24 * HOUR)

    assert fast_low_tier == policy_for(7)["poll_interval_min_seconds"]
    assert fast_low_tier > slow_high_tier


def test_a_measured_rate_inside_the_band_is_used_as_is():
    # Priority 2 allows 30min-2h; one hour sits inside it and passes through.
    assert interval_for(2, gap_seconds=HOUR) == HOUR


def test_no_measurement_falls_back_to_the_tier_default():
    assert interval_for(3, gap_seconds=None) == policy_for(3)["poll_interval_seconds"]


@pytest.mark.django_db
def test_recompute_writes_the_measured_cadence_onto_the_account(settings):
    settings.FETCH_INTERVAL_SAMPLE_DAYS = 30
    user = TwitterUser.objects.create(handle="chigrl", tracking=True, priority=2)
    for index, when in enumerate(_times(12, HOUR)):
        upsert_tweet(
            {"id": str(index), "account": "chigrl", "text": "x", "created_at": when.isoformat()}
        )

    assert recompute_poll_intervals() == 1

    user.refresh_from_db()
    assert user.observed_median_gap_seconds == HOUR
    assert user.poll_interval_seconds == HOUR


@pytest.mark.django_db
def test_recompute_is_idempotent_so_a_daily_run_is_cheap():
    TwitterUser.objects.create(handle="quiet", tracking=True, priority=5)

    assert recompute_poll_intervals() == 1
    assert recompute_poll_intervals() == 0


@pytest.mark.django_db
def test_the_measured_cadence_reaches_the_engine_config():
    """The whole point: the number has to survive the trip into accounts.json."""
    from fetching.accounts import tracked_accounts_payload

    TwitterUser.objects.create(
        handle="business", tracking=True, priority=2, poll_interval_seconds=5400
    )
    TwitterUser.objects.create(handle="unmeasured", tracking=True, priority=2)

    records = {r["username"]: r for r in tracked_accounts_payload()["priority_2"]}

    assert records["business"]["poll_interval_seconds"] == 5400
    assert "poll_interval_seconds" not in records["unmeasured"]
