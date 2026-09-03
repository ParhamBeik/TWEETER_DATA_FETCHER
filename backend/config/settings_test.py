"""Test settings: SQLite + eager Celery so the suite runs with no Postgres,
Redis, or worker. JSONField is supported on SQLite in Django 5.x, which covers
every model here (all JSON columns are plain dict/list blobs).
"""
from __future__ import annotations

import os

# settings.py refuses to boot on a placeholder/short SECRET_KEY. Supply a fixed,
# deliberately non-secret one for tests before importing, so production stays
# strict rather than the check being weakened to accommodate the suite.
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-key-" + "0" * 48)
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost,127.0.0.1")

from .settings import *  # noqa: E402,F401,F403

# SQLite unless a Postgres host is offered.
#
# The suite is meant to run locally with no services, and that stays true. But
# every raw-SQL view -- topics, velocity, narratives, request spend -- checks
# `connection.vendor` and returns early on SQLite, so a green local run says
# nothing whatsoever about them. CI sets TEST_POSTGRES_HOST and gets the real
# thing, including the generated column and the trigram indexes, which behave
# differently on the two databases.
_TEST_POSTGRES_HOST = os.environ.get("TEST_POSTGRES_HOST", "")
if _TEST_POSTGRES_HOST:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("TEST_POSTGRES_DB", "twitter_saas_test"),
            "USER": os.environ.get("TEST_POSTGRES_USER", "postgres"),
            "PASSWORD": os.environ.get("TEST_POSTGRES_PASSWORD", "postgres"),
            "HOST": _TEST_POSTGRES_HOST,
            "PORT": os.environ.get("TEST_POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }

# Run tasks inline; never touch a broker during tests.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "twitter-saas-tests",
    }
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
ALLOW_REGISTRATION = True

# Throttling is wired up but inert: the classes stay installed, every rate is
# None, and a null rate short-circuits SimpleRateThrottle.allow_request.
#
# Rates rather than classes, because DRF binds `APIView.throttle_classes` at
# import time -- emptying the class list here would disable throttling for the
# whole process, and `override_settings` could never turn it back on. Leaving
# the classes installed and neutralising the rates keeps the production wiring
# under test; tests/test_throttling.py sets real rates for the few cases where
# the behaviour is the point.
#
# The counter lives in the process-wide cache, so leaving real rates on here
# would accumulate across unrelated tests -- several of which log in
# legitimately -- and fail whichever one happened to run sixth.
REST_FRAMEWORK = {  # noqa: F405
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_THROTTLE_RATES": {"anon": None, "login": None, "analytics": None},
}

import tempfile  # noqa: E402

MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="twitter-saas-media-"))
MEDIA_URL = "/media/"
