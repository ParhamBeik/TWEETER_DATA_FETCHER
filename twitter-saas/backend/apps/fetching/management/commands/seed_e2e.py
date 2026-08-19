"""Deterministic fixture for the Playwright suite.

Creates the operator account the specs log in with plus a small, fully offline
archive, so end-to-end runs assert on known content and never contact X.
Idempotent: re-running resets the fixture rather than duplicating it.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.tweets.models import Search, SearchResult, Tweet, TwitterUser

USERNAME = "e2e-operator"
PASSWORD = "e2e-password-123"

ACCOUNTS = [
    ("alpha_signal", "Alpha Signal", 1, True, False),
    ("beta_watch", "Beta Watch", 3, True, False),
    ("gamma_muted", "Gamma Muted", 5, False, False),
    ("delta_blocked", "Delta Blocked", 7, True, True),
]

TWEETS = [
    ("alpha_signal", "1001", "Falcon launch cadence is accelerating this quarter."),
    ("alpha_signal", "1002", "Second stage recovered intact after the burn."),
    ("beta_watch", "1003", "Market volatility spiked on the announcement."),
    ("beta_watch", "1004", "Follow-up thread on the volatility numbers."),
    ("gamma_muted", "1005", "This account is not currently tracked."),
]


class Command(BaseCommand):
    help = "Seed a deterministic, offline fixture for the end-to-end suite."

    def handle(self, *args, **options):
        User = get_user_model()
        User.objects.filter(username=USERNAME).delete()
        User.objects.create_user(username=USERNAME, password=PASSWORD)

        Tweet.objects.all().delete()
        SearchResult.objects.all().delete()
        Search.objects.all().delete()
        TwitterUser.objects.all().delete()

        users = {}
        for handle, display, priority, tracking, quarantined in ACCOUNTS:
            users[handle] = TwitterUser.objects.create(
                handle=handle,
                display_name=display,
                priority=priority,
                tracking=tracking,
                quarantined=quarantined,
                quarantine_reason="user id unresolved" if quarantined else "",
                rest_id=f"rest-{handle}",
            )

        now = timezone.now()
        for index, (handle, tweet_id, text) in enumerate(TWEETS):
            Tweet.objects.create(
                dedup_key=f"rest-{handle}:{tweet_id}",
                tweet_id=tweet_id,
                author_rest_id=f"rest-{handle}",
                account=handle,
                author=users[handle],
                text=text,
                url=f"https://x.com/{handle}/status/{tweet_id}",
                # Descending so the feed order is deterministic.
                created_at=now - timedelta(minutes=index * 10 + 1),
                likes=100 - index,
                retweets=50 - index,
                replies=10,
                views=1000,
            )

        search = Search.objects.create(
            name="Launch coverage",
            slug="launch-coverage",
            raw_query="launch lang:en",
            product="Top",
            pagination_depth=1,
        )
        SearchResult.objects.create(
            search=search,
            tweet=Tweet.objects.get(tweet_id="1001"),
            rank=0,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded e2e fixture: {TwitterUser.objects.count()} accounts, "
                f"{Tweet.objects.count()} tweets, login {USERNAME}."
            )
        )
