"""Request guards that belong in front of every view rather than inside one."""
from __future__ import annotations

from django.http import JsonResponse

NUL = "\x00"


class RejectNullBytesMiddleware:
    """Refuse any request whose query string carries a NUL byte.

    Postgres text columns cannot hold a NUL, so psycopg raises `DataError` the
    moment one reaches the driver as a bind parameter -- and every string filter
    on this API passes a request value straight into a `filter()`. `?q=%00`,
    `?account=%00`, `?run_id=%00`, `?subsystem=%00` and `?product=%00` were each
    a 500.

    Rejecting at the edge rather than stripping at ~20 call sites: a NUL in a
    URL is never a value anyone meant to send, there is nothing in the archive
    it could ever match, and one guard cannot be forgotten by the next filter
    someone adds. Django already blocks NULs in the *path* for the same reason;
    this extends that to the query string.

    SQLite accepts NULs happily, so the whole class of failure is invisible to a
    local test run and only appears in production. tests/test_api_input_sweep.py
    covers it on both databases.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Checked after decoding, not against QUERY_STRING: a NUL arrives on the
        # wire percent-encoded as %00, so the raw string never contains one and
        # scanning it would pass everything through.
        if any(NUL in value for value in _query_values(request)):
            return JsonResponse(
                {"detail": "Query string contains a NUL byte."}, status=400
            )
        return self.get_response(request)


def _query_values(request):
    """Every decoded query-string value, including repeated keys."""
    for key in request.GET:
        yield key
        yield from request.GET.getlist(key)
