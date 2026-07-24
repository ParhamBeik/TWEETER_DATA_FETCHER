"""Test settings: SQLite + eager Celery so the suite runs with no Postgres,
Redis, or worker. JSONField is supported on SQLite in Django 5.x, which covers
every model here (all JSON columns are plain dict/list blobs).
"""
from __future__ import annotations

from .settings import *  # noqa: F401,F403

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

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
