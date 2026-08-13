"""Project-wide cursor pagination.

Tweet feeds order by tweet time then id. FetchRun lists use started_at.
"""
from rest_framework.pagination import CursorPagination


class StandardCursorPagination(CursorPagination):
    ordering = ("-created_at", "-id")
    page_size = 30


class FetchRunCursorPagination(CursorPagination):
    ordering = ("-started_at", "-id")
    page_size = 30
