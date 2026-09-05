"""Reading untrusted values off a request.

DRF parses a request body into whatever JSON it held -- an object, but equally a
list, a string, a number or a boolean -- and hands the result to the view as
`request.data`. Every view here that reads a named field assumed a mapping, so
`curl -d '[]' -H 'Content-Type: application/json' .../api/auth/register/`
raised AttributeError and answered 500 instead of 400, from an endpoint that is
open to unauthenticated callers.
"""
from __future__ import annotations

from collections.abc import Mapping

from rest_framework.exceptions import ParseError


def body_mapping(request) -> Mapping:
    """`request.data` when it is a set of named fields; otherwise a 400.

    Returned unchanged rather than copied, so callers keep `.get`, `in` and --
    for the QueryDict a form-encoded or multipart request produces -- repeatable
    keys. QueryDict and dict are both Mappings, which is the whole test.

    ParseError rather than ValidationError: the shape of the body is wrong, not
    the value of a field, and it renders `{"detail": "..."}` as a plain string,
    which is the error shape the console reads.
    """
    data = request.data
    if isinstance(data, Mapping):
        return data
    raise ParseError(
        f"Expected a JSON object of fields, got {type(data).__name__}."
    )
