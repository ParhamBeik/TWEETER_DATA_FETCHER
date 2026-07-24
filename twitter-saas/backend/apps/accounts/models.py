"""Per-user follow lists and search subscriptions."""
from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.tweets.models import Search, TwitterUser


class Follow(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="follows")
    account = models.ForeignKey(TwitterUser, on_delete=models.CASCADE, related_name="followers")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "account")]


class SearchSubscription(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="search_subs")
    search = models.ForeignKey(Search, on_delete=models.CASCADE, related_name="subscribers")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "search")]
