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
