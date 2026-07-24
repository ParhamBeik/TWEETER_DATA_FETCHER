"""Django settings for the twitter-saas backend.

Standalone project: no imports from or paths into the origin twitter_fetcher repo.
The vendored fetcher lives under apps/fetching/vendor and is made importable as
the top-level package ``tweeter_data_fetcher`` via sys.path below.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Make the vendored fetcher importable as ``tweeter_data_fetcher``.
VENDOR_DIR = BASE_DIR / "apps" / "fetching" / "vendor"
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

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

# Fetcher runtime knobs.
FETCH_LIVE_INTERVAL_SECONDS = int(os.environ.get("FETCH_LIVE_INTERVAL_SECONDS", "600"))
FETCH_HISTORICAL_INTERVAL_SECONDS = int(os.environ.get("FETCH_HISTORICAL_INTERVAL_SECONDS", "21600"))
FETCH_SEARCH_INTERVAL_SECONDS = int(os.environ.get("FETCH_SEARCH_INTERVAL_SECONDS", "1800"))
# ponytail: naive unbounded tracked-account growth; add per-run cap / eviction when the set gets large.
FETCH_MAX_ACCOUNTS_PER_RUN = int(os.environ.get("FETCH_MAX_ACCOUNTS_PER_RUN", "25"))
