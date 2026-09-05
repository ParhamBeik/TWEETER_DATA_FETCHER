"""Every route, every parameter, every hostile value: nothing may answer 5xx.

A 4xx here is fine -- rejecting bad input is the point. A 5xx means a request
someone can type into the URL bar reached an unhandled exception, which is both
an error the console cannot explain to its user and a line of noise in the logs
that hides real failures.

The sweep is table-driven so adding a route or a parameter is one line, and so
the audit stays exhaustive as the surface grows rather than sampling whatever
was interesting the day it was written.
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from tweets.models import TwitterUser

NUL = chr(0)

# Values a hand-edited URL, a stale bookmark, or a fuzzer can produce.
HOSTILE_VALUES = [
    "", " ", "0", "-1", "abc", "null", "None", "true", "1.5",
    "9" * 30, "-" + "9" * 30,     # integer overflow
    "1e309", "nan", "inf", "-inf",  # float edge cases
    "0001-01-01T00:00:00Z",        # datetime underflow: subtracting a window overflows
    "9999-12-31T23:59:59Z",        # datetime overflow
    "0001-01-01",
    "99999999999d", "9999999999999999999h",  # timedelta overflow
    "999999999d",   # the largest representable timedelta: subtracting it is what overflows
    NUL, "before" + NUL + "after",
    "' OR 1=1--", "'; DROP TABLE tweets_tweet; --",
    "../../etc/passwd", "<script>alert(1)</script>",
    "a" * 5000, "🙂", ",,,,", "hour; DROP TABLE tweets_tweet",
]

# Every GET route, mapped to every query parameter its view reads.
GET_ROUTES = {
    "/api/feed/": [
        "account", "tier", "since", "until", "window", "run_id", "q", "types",
        "has_media", "include_untracked", "sort", "cursor", "limit", "offset",
    ],
    "/api/accounts/": ["q", "tracking"],
    "/api/accounts/someone/tweets/": ["cursor"],
    "/api/runs/": ["subsystem", "cursor"],
    "/api/searches/": ["product", "cursor"],
    "/api/export/": [],
    "/api/session/": [],
    "/api/stats/overview/": ["range", "since", "until", "bucket", "account"],
    "/api/stats/pipeline/": [],
    "/api/analytics/ingestion/": ["range", "since", "until", "bucket", "account"],
    "/api/analytics/velocity/": ["range", "since", "until", "bucket", "account"],
    "/api/analytics/topics/": [
        "range", "since", "until", "bucket", "account", "dimension", "rank",
    ],
    "/api/analytics/topics/hidden/": [],
    "/api/analytics/accounts/": ["range", "since", "until", "bucket", "account"],
    "/api/analytics/narratives/": [
        "range", "since", "until", "bucket", "account", "window_hours",
        "similarity", "min_length", "limit", "candidates",
    ],
}

# Detail routes whose captured URL segment is itself untrusted input.
PATH_ROUTES = [
    "/api/runs/{}/",
    "/api/export/{}/",
    "/api/export/{}/download/",
    "/api/accounts/{}/tweets/",
    "/api/accounts/{}/",
    "/api/searches/{}/",
    "/api/searches/{}/results/",
    "/api/searches/{}/runs/",
    "/api/searches/{}/schedule/",
]

POST_ROUTES = [
    "/api/cycles/",
    "/api/session/",
    "/api/accounts/",
    "/api/searches/",
    "/api/export/",
    "/api/analytics/topics/hidden/",
]

# The routes an unauthenticated caller can reach. Their bodies are the only
# request bodies on this service that arrive from outside the login wall.
ANONYMOUS_POST_ROUTES = [
    "/api/auth/register/",
    "/api/auth/login/",
    "/api/auth/logout/",
    "/api/auth/refresh/",
]

PATCH_ROUTES = ["/api/accounts/sweep-target/"]

# Valid JSON that is not the shape the view assumes. DRF hands `request.data`
# straight through, so a list reaches a view that calls `.get` on it.
MALFORMED_BODIES = [[], [1, 2, 3], [{"handle": "x"}], "a string", 12345, True]


@pytest.fixture
def staff_client(db):
    user = get_user_model().objects.create_user(
        "sweep-auditor", password="unused-in-force-auth", is_staff=True
    )
    # A real row behind PATCH_ROUTES, so a malformed PATCH body reaches the view
    # instead of stopping at the 404 an absent account would produce.
    TwitterUser.objects.create(handle="sweep-target", display_name="Sweep Target")
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _failures(client, calls):
    """Run (method, path, kwargs) triples, collecting anything 5xx or raising."""
    found = []
    for method, path, label, kwargs in calls:
        try:
            response = getattr(client, method)(path, **kwargs)
        except Exception as exc:  # an escaped exception is worse than a 500
            found.append(f"{method.upper()} {path} [{label}] raised {exc!r}")
            continue
        if response.status_code >= 500:
            found.append(f"{method.upper()} {path} [{label}] -> {response.status_code}")
    return found


@pytest.mark.parametrize("path", sorted(GET_ROUTES))
def test_query_parameters_never_500(staff_client, path):
    calls = [("get", path, "no params", {})]
    for param in GET_ROUTES[path]:
        for value in HOSTILE_VALUES:
            calls.append(("get", path, f"{param}={value[:40]!r}", {"data": {param: value}}))
    assert not _failures(staff_client, calls)


def test_path_segments_never_500(staff_client):
    calls = []
    for template in PATH_ROUTES:
        for value in HOSTILE_VALUES:
            # A URL cannot carry a raw NUL or a slash in one segment; the point
            # is what the view does with the segment, not what a client can send.
            segment = value.replace(NUL, "").replace("/", "%2F") or "x"
            calls.append(("get", template.format(segment), repr(value[:40]), {}))
    assert not _failures(staff_client, calls)


def test_malformed_request_bodies_never_500(staff_client):
    calls = [
        ("post", path, repr(body), {"data": body, "format": "json"})
        for path in POST_ROUTES
        for body in MALFORMED_BODIES
    ]
    calls += [
        ("patch", path, repr(body), {"data": body, "format": "json"})
        for path in PATCH_ROUTES
        for body in MALFORMED_BODIES
    ]
    assert not _failures(staff_client, calls)


def test_anonymous_request_bodies_never_500(db):
    """The pre-login endpoints, which take bodies from unauthenticated callers."""
    calls = [
        ("post", path, repr(body), {"data": body, "format": "json"})
        for path in ANONYMOUS_POST_ROUTES
        for body in MALFORMED_BODIES
    ]
    assert not _failures(APIClient(), calls)


def test_nul_byte_in_a_query_value_is_rejected(staff_client):
    """A NUL is a psycopg DataError, not a filter value -- so it stops at 400.

    Asserted on the status rather than only on "not 5xx" because SQLite accepts
    NULs and would happily answer 200, which is how this stayed invisible until
    the sweep was run against Postgres.
    """
    for path, param in [
        ("/api/feed/", "q"),
        ("/api/accounts/", "q"),
        ("/api/runs/", "subsystem"),
        ("/api/searches/", "product"),
        ("/api/analytics/topics/", "account"),
    ]:
        response = staff_client.get(path, {param: "before" + NUL + "after"})
        assert response.status_code == 400, f"{path}?{param}= gave {response.status_code}"


def test_interacting_parameters_never_500(staff_client):
    """Pairs that only misbehave together: a window's two ends, a sort and its paginator."""
    combos = [
        ("/api/analytics/ingestion/", {"since": "9999-12-31T00:00:00Z", "until": "0001-01-01T00:00:00Z"}),
        ("/api/analytics/ingestion/", {"until": "0001-01-01T00:00:00Z"}),
        ("/api/analytics/ingestion/", {"since": "0001-01-01T00:00:00Z", "until": "9999-12-31T00:00:00Z"}),
        ("/api/analytics/topics/", {"range": "99999999999d"}),
        ("/api/analytics/narratives/", {"until": "0001-01-02T00:00:00Z"}),
        ("/api/stats/overview/", {"until": "0001-01-01T00:00:00Z", "range": "90d"}),
        ("/api/feed/", {"sort": "top", "offset": "9" * 20}),
        ("/api/feed/", {"sort": "views", "limit": "9" * 20}),
        ("/api/feed/", {"since": "9999-12-31T23:59:59Z", "until": "0001-01-01T00:00:00Z"}),
        ("/api/feed/", {"window": "today", "sort": "top", "types": "tweet,reply"}),
        ("/api/feed/", {"tier": "9" * 30}),
    ]
    calls = [("get", path, str(params), {"data": params}) for path, params in combos]
    assert not _failures(staff_client, calls)
