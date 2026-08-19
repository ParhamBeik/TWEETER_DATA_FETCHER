"""End-to-end settings: a real HTTP server backed by file-based SQLite.

Differs from `settings_test` in two ways that matter:

* The database is a file, not `:memory:`. The seed step and the server run in
  separate processes, so an in-memory database would be empty by the time
  Playwright connected.
* Celery is **not** eager. Queuing a cycle from the UI must record the intent
  without executing a pipeline -- an eager task would launch the real fetcher
  subprocess and hit X during a test run.
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DJANGO_SECRET_KEY", "e2e-only-key-" + "0" * 48)
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost,127.0.0.1")

from .settings import *  # noqa: E402,F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": Path(BASE_DIR) / "e2e.sqlite3",  # noqa: F405
    }
}

# Enqueue without executing: nothing consumes the in-memory transport, so the
# API still returns 202 and the UI flow is exercised with no X traffic.
CELERY_TASK_ALWAYS_EAGER = False
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
CELERY_TASK_ALWAYS_EAGER_PROPAGATES = False

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "twitter-saas-e2e",
    }
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
ALLOW_REGISTRATION = True
