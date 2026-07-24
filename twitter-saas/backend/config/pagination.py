"""Project-wide cursor pagination.

DRF's CursorPagination defaults to ordering by "-created", which no model here
defines, and it requires a unique, non-null, monotonic field. `created_at` is
nullable and non-unique, so we paginate on the auto-increment `-id` (which also
tracks ingestion/arrival order — newest first).
"""
from rest_framework.pagination import CursorPagination


class StandardCursorPagination(CursorPagination):
    ordering = "-id"
    page_size = 30
