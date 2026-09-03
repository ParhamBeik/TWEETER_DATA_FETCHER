"""Project-wide cursor pagination.

Tweet feeds order by tweet time then id. FetchRun lists use started_at.
"""
from collections import OrderedDict

from rest_framework.exceptions import ValidationError
from rest_framework.pagination import CursorPagination, LimitOffsetPagination
from rest_framework.response import Response
from rest_framework.utils.urls import remove_query_param, replace_query_param


class StandardCursorPagination(CursorPagination):
    # Orders on the `feed_ts` annotation, not `created_at` directly: created_at is
    # nullable (X sometimes returns an unparseable timestamp) and cursor paging
    # compares with __lt, which never matches NULL. Tweet querysets that use this
    # class must apply tweets.views.with_feed_ts().
    ordering = ("-feed_ts", "-id")
    page_size = 30


class FeedOffsetPagination(LimitOffsetPagination):
    """For feed orderings a cursor cannot express, i.e. sorting by engagement.

    A cursor encodes the ordering field's value, so it needs a unique, monotonic
    column; engagement and views are neither, since thousands of tweets share a
    score of zero. That leaves offset paging, which has two costs that grow with
    the archive rather than with the page:

    * ``LimitOffsetPagination`` issues a ``COUNT(*)`` over the whole filtered
      set on *every* page, purely to render a total.
    * ``OFFSET n`` makes the database walk and discard n rows.

    This class removes the first and bounds the second. The previous version
    assumed these requests were "bounded by a time window" -- they are not: the
    console offers All time as a one-click option next to the ranked sorts.

    The response keeps the same shape, minus a meaningful ``count``. Nothing in
    the frontend reads it for these sorts; the feed appends pages until ``next``
    is null.
    """

    default_limit = 30
    max_limit = 100
    # Deep pages of a ranked feed are not a real question anyone asks, and this
    # is the only bound that stops OFFSET growing without limit.
    max_offset = 1000

    def paginate_queryset(self, queryset, request, view=None):
        self.limit = self.get_limit(request)
        if self.limit is None:
            return None
        self.offset = self.get_offset(request)
        self.request = request
        if self.offset > self.max_offset:
            raise ValidationError(
                {
                    "detail": (
                        f"This sort pages to a maximum offset of {self.max_offset}. "
                        "Narrow the time window or add filters to reach older results."
                    )
                }
            )
        # One row beyond the page: enough to know whether a next page exists,
        # which is all the response actually needs, and far cheaper than
        # counting the archive to find out.
        rows = list(queryset[self.offset : self.offset + self.limit + 1])
        self.has_next = len(rows) > self.limit and (self.offset + self.limit <= self.max_offset)
        return rows[: self.limit]

    def get_next_link(self):
        if not self.has_next:
            return None
        url = self.request.build_absolute_uri()
        url = replace_query_param(url, self.limit_query_param, self.limit)
        return replace_query_param(url, self.offset_query_param, self.offset + self.limit)

    def get_previous_link(self):
        if self.offset <= 0:
            return None
        url = self.request.build_absolute_uri()
        url = replace_query_param(url, self.limit_query_param, self.limit)
        offset = self.offset - self.limit
        if offset <= 0:
            return remove_query_param(url, self.offset_query_param)
        return replace_query_param(url, self.offset_query_param, offset)

    def get_paginated_response(self, data):
        # `count` stays in the envelope so the response shape does not change
        # between sorts, but it is null rather than a number this paginator
        # deliberately no longer computes. Reporting the page length here would
        # be worse: it reads as a total and is not one.
        return Response(
            OrderedDict(
                [
                    ("count", None),
                    ("next", self.get_next_link()),
                    ("previous", self.get_previous_link()),
                    ("results", data),
                ]
            )
        )


class FetchRunCursorPagination(CursorPagination):
    ordering = ("-started_at", "-id")
    page_size = 30


class CreatedAtCursorPagination(CursorPagination):
    """For non-Tweet models whose own `created_at` is non-null (e.g. Search)."""

    ordering = ("-created_at", "-id")
    page_size = 30
