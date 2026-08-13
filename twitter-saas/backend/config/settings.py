"""Django settings for the twitter-saas backend.

Standalone project: the canonical fetcher lives under twitter_fetcher/src and
is made importable as ``tweeter_data_fetcher`` via PYTHONPATH (Docker) or the
repo-relative path below (local pytest / manage.py).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Prefer image copy (/app/fetcher); fall back to repo layout for local runs.
_FETCHER_CANDIDATES = [
    Path(os.environ["TDF_FETCHER_SRC"]) if os.environ.get("TDF_FETCHER_SRC") else None,
    Path("/app/fetcher"),
    BASE_DIR.parents[1] / "twitter_fetcher" / "src",  # repo_root/twitter_fetcher/src
]
for _candidate in _FETCHER_CANDIDATES:
    if _candidate is not None and _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))
        break

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "apps.accounts",
    "apps.tweets",
    "apps.fetching",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "twitter_saas"),
        "USER": os.environ.get("POSTGRES_USER", "postgres"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "postgres"),
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# collectstatic target. Required for /admin/ static assets when served behind
# gunicorn (DEBUG=0); harmless under DEBUG=1.
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "config.pagination.StandardCursorPagination",
    "PAGE_SIZE": 30,
}

CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = [
    o for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if o
]

# Celery / Redis
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1")
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_REJECT_ON_WORKER_LOST = True

# LocMem for local pytest; Docker sets DJANGO_CACHE_URL so cycle locks share Redis.
_DJANGO_CACHE_URL = os.environ.get("DJANGO_CACHE_URL", "")
if _DJANGO_CACHE_URL.startswith("redis://"):
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _DJANGO_CACHE_URL,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "twitter-saas",
        }
    }

# Fetcher runtime knobs.
FETCH_LIVE_INTERVAL_SECONDS = int(os.environ.get("FETCH_LIVE_INTERVAL_SECONDS", "1200"))
FETCH_HISTORICAL_INTERVAL_SECONDS = int(os.environ.get("FETCH_HISTORICAL_INTERVAL_SECONDS", "21600"))
FETCH_SEARCH_INTERVAL_SECONDS = int(os.environ.get("FETCH_SEARCH_INTERVAL_SECONDS", "1800"))
FETCH_CYCLE_TIMEOUT_SECONDS = int(os.environ.get("FETCH_CYCLE_TIMEOUT_SECONDS", "1800"))
FETCH_MAX_ACCOUNTS_PER_RUN = int(os.environ.get("FETCH_MAX_ACCOUNTS_PER_RUN", "80"))
SEARCH_TWEET_TTL_DAYS = int(os.environ.get("SEARCH_TWEET_TTL_DAYS", "30"))
FETCH_RUN_RETENTION_DAYS = int(os.environ.get("FETCH_RUN_RETENTION_DAYS", "90"))
ALLOW_REGISTRATION = os.environ.get("ALLOW_REGISTRATION", "0") == "1"
