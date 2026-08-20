"""Project-wide cursor pagination.

Tweet feeds order by tweet time then id. FetchRun lists use started_at.
"""
from rest_framework.pagination import CursorPagination


class StandardCursorPagination(CursorPagination):
    # Orders on the `feed_ts` annotation, not `created_at` directly: created_at is
    # nullable (X sometimes returns an unparseable timestamp) and cursor paging
    # compares with __lt, which never matches NULL. Tweet querysets that use this
    # class must apply tweets.views.with_feed_ts().
    ordering = ("-feed_ts", "-id")
    page_size = 30


class FetchRunCursorPagination(CursorPagination):
    ordering = ("-started_at", "-id")
    page_size = 30


class CreatedAtCursorPagination(CursorPagination):
    """For non-Tweet models whose own `created_at` is non-null (e.g. Search)."""

    ordering = ("-created_at", "-id")
    page_size = 30
