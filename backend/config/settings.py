"""Django settings.

The X engine (``fetcher/``) is a package inside this project, so it needs no
path wiring: the workers run it as ``python -m fetcher.<pipeline>``.
"""
from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = os.environ.get(
    "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1"
).split(",")

# Placeholder keys shipped in .env.example were being used verbatim in .env, so
# session cookies and DRF tokens were forgeable by anyone with the repo. Refuse
# to boot rather than run on a known-public key.
_PLACEHOLDER_KEYS = {
    "",
    "dev-insecure-change-me",
    "change-me-to-a-long-random-string",
}
if SECRET_KEY in _PLACEHOLDER_KEYS or len(SECRET_KEY) < 32:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY is unset, a placeholder, or too short. Generate one:\n"
        "  python -c \"import secrets; print(secrets.token_urlsafe(64))\""
    )

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "tweets",
    "fetching",
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

# Signup is open, so the password rules are the only thing standing between a
# weak password and an account on a system that drives a shared X session.
# MinimumLength alone accepted "password1" and any handle already in the repo.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# collectstatic target. Required for /admin/ static assets when served behind
# gunicorn (DEBUG=0); harmless under DEBUG=1.
STATIC_ROOT = BASE_DIR / "staticfiles"
# Archived photos. A compose volume in production; never the fetcher scratch
# tree, which is deleted after every run.
# `or` rather than a get() default: compose passes an unset .env key through as
# an empty string, and Path("") resolves to the working directory -- which would
# scatter archived media wherever the process happened to start.
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT") or (BASE_DIR / "media"))
MEDIA_URL = "/media/"
# Photos per archive_media tick. The control worker is solo; a giant job would
# starve the search dispatcher the same way a shared queue once did.
MEDIA_ARCHIVE_BATCH = int(os.environ.get("MEDIA_ARCHIVE_BATCH", "25"))
MEDIA_ARCHIVE_INTERVAL_SECONDS = int(os.environ.get("MEDIA_ARCHIVE_INTERVAL_SECONDS", "120"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        # Session auth stays for the Django admin's browsable pages. The old
        # TokenAuthentication is gone: its tokens never expire, so a leaked one
        # is valid until someone notices and deletes the row by hand.
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "config.pagination.StandardCursorPagination",
    "PAGE_SIZE": 30,
    # Nothing here was rate limited at all, which meant unlimited password
    # attempts against a system that drives a shared X session.
    #
    # AnonRateThrottle covers unauthenticated traffic generally; ScopedRateThrottle
    # applies only to views that declare a `throttle_scope`, so signed-in console
    # use is deliberately unmetered -- the budget rail, ops and feed pollers make
    # ~500 requests/hour per open tab, and a user ceiling low enough to be useful
    # would throttle the UI itself.
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.environ.get("THROTTLE_ANON", "60/min"),
        # Five a minute is invisible to a person typing a password and makes
        # guessing at any useful speed impossible.
        "login": os.environ.get("THROTTLE_LOGIN", "5/min"),
        # The analytics views run the heavy raw SQL -- trigram self-joins, phrase
        # mining, window functions over the metric table. These are the requests
        # worth metering even for a signed-in user.
        "analytics": os.environ.get("THROTTLE_ANALYTICS", "30/min"),
    },
}

from datetime import datetime, timedelta  # noqa: E402  (kept next to the settings it configures)

SIMPLE_JWT = {
    # Short access token, long refresh: a stolen access token expires on its
    # own, and a stolen refresh token can be revoked.
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    # Each refresh issues a new refresh token and blacklists the one used, so a
    # replayed refresh token is dead on arrival.
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}

# --- Serving behind a TLS-terminating proxy ---------------------------------
#
# In production Caddy terminates HTTPS on the host and proxies to the frontend
# nginx over plain HTTP, which proxies /api/ to gunicorn. Django therefore sees
# an http:// request and, without the header below, believes the connection was
# never encrypted -- which makes request.is_secure() false, secure cookies
# pointless, and CSRF origin checks disagree with the browser.
#
# nginx forwards the scheme Caddy reported (see frontend/nginx.conf), so this
# header is the proxy's word, not the client's.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Off by default so `runserver` and the plain-HTTP compose stack keep working;
# set DJANGO_SECURE_SSL=1 in the production .env, where TLS really is terminated
# upstream. Marking a cookie Secure on a site served over http makes the browser
# discard it, so this cannot be unconditional.
_SECURE_SSL = os.environ.get("DJANGO_SECURE_SSL", "0") == "1"
SESSION_COOKIE_SECURE = _SECURE_SSL
CSRF_COOKIE_SECURE = _SECURE_SSL
SESSION_COOKIE_HTTPONLY = True
# The admin is the only session-cookie surface here and it is same-origin, so
# Lax costs nothing and blocks cross-site form posts.
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_HSTS_SECONDS = 31536000 if _SECURE_SSL else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = _SECURE_SSL
SECURE_HSTS_PRELOAD = _SECURE_SSL
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CONTENT_TYPE_NOSNIFF = True
# SECURE_SSL_REDIRECT stays off deliberately, and `manage.py check --deploy`
# will keep saying so. Caddy already answers :80 with a redirect, so Django
# redirecting as well only adds a hop -- and if the proxy header were ever
# misconfigured, an app-level redirect turns that into an infinite loop instead
# of a page served over the wrong scheme.
# Django >= 4 checks the Origin header against this list on every unsafe request,
# and an https:// origin never matches an http:// ALLOWED_HOSTS entry -- so
# without it the admin login form 403s the moment it is served over HTTPS.
# Comma-separated, scheme included: https://your.domain
CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if o.strip()
]

# Never all-origins: this API is token-authenticated and drives a shared X
# session, so an arbitrary origin must not be able to call it.
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = [
    o
    for o in os.environ.get(
        # :5173 = vite dev server, :8080 = nginx frontend in compose.
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:8080",
    ).split(",")
    if o
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
# Redis _cycle_lock prevents overlapping workers, so ticks may be shorter than
# the cycle timeout. The live scheduler admits only the current rate-budget slice.
#
# Priority is archive completeness over live freshness: the worker runs -P
# solo --concurrency=1, so every task is serialized, and scheduling frequency
# alone determines who gets worker time regardless of how fast each cycle
# runs. Live used to tick 6x more often than historical/search (300s vs
# 1800s) and crowded them out simply by being queued far more often. Live is
# now on the same 1800s cadence as search; historical ticks far more
# frequently (300s) than either, since it's the thing we now want to make
# the most progress.
FETCH_LIVE_INTERVAL_SECONDS = int(os.environ.get("FETCH_LIVE_INTERVAL_SECONDS", "1800"))
# Historical backfill used to try every tracked account in one run every
# FETCH_HISTORICAL_INTERVAL_SECONDS and get SIGKILLed by FETCH_CYCLE_TIMEOUT_SECONDS
# partway through -- the worker runs -P solo --concurrency=1, so one unbounded
# backfill run starves live/search fetching for as long as it takes, and killing
# it hard threw away all progress from that tick. It now processes a bounded
# FETCH_HISTORICAL_CHUNK_SIZE accounts per tick, oldest-backfilled-first (see
# fetching.tasks.backfill_historical_all), so it ticks far more often and
# each tick finishes well under the timeout instead of racing it.
#
# Chunk size dropped 12 -> 4 -> 1. 4 wasn't enough either: oldest-backfilled-
# first ordering has no idea which accounts are high-volume, so it can still
# deal a chunk two heavy accounts at once (observed: @business + @reuters
# together still blew the full 1800s budget and got SIGKILLed, same stuck-
# forever symptom as the original 12-account chunks, just rarer). A chunk of
# 1 makes this impossible by construction -- worst case is the single
# heaviest account's own pagination + rate-limit cooldown, which historically
# fits in ~15-20 minutes, comfortably inside the 1800s ceiling. Ticking every
# FETCH_HISTORICAL_INTERVAL_SECONDS still processes accounts back-to-back;
# _cycle_lock just makes an overlapping tick a cheap no-op skip instead of a
# second fetcher racing the first.
FETCH_HISTORICAL_INTERVAL_SECONDS = int(os.environ.get("FETCH_HISTORICAL_INTERVAL_SECONDS", "300"))
FETCH_HISTORICAL_CHUNK_SIZE = int(os.environ.get("FETCH_HISTORICAL_CHUNK_SIZE", "1"))
# Pages one account may fetch per backfill tick. The archive walk keeps its own
# cursor and resumes where the previous tick stopped, so this bounds a tick, not
# the account: a 900-tweet timeline finishes over a few ticks instead of being
# retried from page 1 forever. Passed to the engine subprocess by
# fetching.runner.run_fetcher as TDF_HISTORICAL_PAGES_PER_TICK.
FETCH_HISTORICAL_PAGES_PER_TICK = int(os.environ.get("FETCH_HISTORICAL_PAGES_PER_TICK", "25"))
# Requests the archive walk must leave in the shared UserTweets bucket for the
# live poller (which reserves 5 more for itself). With no floor the backfill
# drained the bucket every tick and live deferred 100% of its accounts.
FETCH_HISTORICAL_QUOTA_FLOOR = int(os.environ.get("FETCH_HISTORICAL_QUOTA_FLOOR", "20"))
# How far back the archive walk is willing to go, as an ISO date. This is a
# deliberate storage ceiling, not a technical limit: an unbounded walk over 64
# accounts collects millions of tweets nobody asked for. Reaching it is a real
# completion -- the only other honest one is X refusing to page further, which
# is a *provider* limit and must not be recorded as "we have everything".
#
# Lowering this invalidates every account already marked `reached_date_floor`:
# they stopped at the old floor and will not resume on their own. Reopen them
# with `manage.py reopen_shallow_archives` after changing it. Each completion
# records the floor it was judged against (`backfill_floor_date`) so the
# affected accounts can be found.
FETCH_ARCHIVE_EARLIEST_DATE = os.environ.get("FETCH_ARCHIVE_EARLIEST_DATE", "2024-01-01")
# Validated at import so a typo fails the container's boot loudly, rather than
# reaching the fetcher subprocess where the only signal is a line in a log.
try:
    datetime.strptime(FETCH_ARCHIVE_EARLIEST_DATE, "%Y-%m-%d")
except ValueError as exc:
    raise ImproperlyConfigured(
        f"FETCH_ARCHIVE_EARLIEST_DATE={FETCH_ARCHIVE_EARLIEST_DATE!r} is not YYYY-MM-DD"
    ) from exc
# Fallback cadence for a Search created without one. Per-search cadence now
# lives on the row itself (Search.interval_seconds).
FETCH_SEARCH_INTERVAL_SECONDS = int(os.environ.get("FETCH_SEARCH_INTERVAL_SECONDS", "1800"))
# How often the dispatcher checks which searches are due. This is not the fetch
# cadence -- it only needs to be fine-grained enough that a search due at T
# starts shortly after T.
FETCH_SEARCH_DISPATCH_SECONDS = int(os.environ.get("FETCH_SEARCH_DISPATCH_SECONDS", "300"))
FETCH_CYCLE_TIMEOUT_SECONDS = int(os.environ.get("FETCH_CYCLE_TIMEOUT_SECONDS", "1800"))
FETCH_MAX_ACCOUNTS_PER_RUN = int(os.environ.get("FETCH_MAX_ACCOUNTS_PER_RUN", "100"))
# How far back recompute_poll_intervals looks when measuring an account's real
# posting rate. Long enough to survive a quiet week, short enough that an
# account which changed its habits is re-tiered within the month.
FETCH_INTERVAL_SAMPLE_DAYS = int(os.environ.get("FETCH_INTERVAL_SAMPLE_DAYS", "30"))
SEARCH_TWEET_TTL_DAYS = int(os.environ.get("SEARCH_TWEET_TTL_DAYS", "30"))
FETCH_RUN_RETENTION_DAYS = int(os.environ.get("FETCH_RUN_RETENTION_DAYS", "90"))
# Raw pages get a shorter clock than the runs that produced them: they are the
# bulk of the database (~750 MB/day) and their usefulness decays much faster.
# TEMPORARY: 3, not 30, for as long as every run still writes every page.
# fetching.runner logs a `raw_page_census` line per run; once a week of those
# says which statuses are worth keeping, set RAW_PAGE_KEEP_STATUSES there and
# restore this to 30. Three days bounds the table at ~2 GB in the meantime,
# where 30 would let it return to ~22 GB while we are still measuring.
# The default carries the temporary value on purpose: a production .env edit
# that has to be remembered is one that gets forgotten.
RAW_PAGE_RETENTION_DAYS = int(os.environ.get("RAW_PAGE_RETENTION_DAYS", "3"))
# Ceiling per purge run. The first pass after deploy has a multi-GB backlog and
# shares a worker with the search dispatcher; the remainder expires tomorrow.
RAW_PAGE_PURGE_MAX_ROWS = int(os.environ.get("RAW_PAGE_PURGE_MAX_ROWS", "200000"))
# Engagement snapshots. Matched to the analytics window ceiling
# (analytics.MAX_WINDOW_HOURS is 90 days) rather than chosen independently: a
# snapshot older than the longest window any endpoint will serve cannot appear
# in a chart, so this deletes only rows no query could reach. Until now this
# table had no clock at all -- ingest writes a row every time a re-poll sees a
# changed like/repost/view count, and views tick constantly on a popular post.
TWEET_METRIC_RETENTION_DAYS = int(os.environ.get("TWEET_METRIC_RETENTION_DAYS", "90"))
TWEET_METRIC_PURGE_MAX_ROWS = int(os.environ.get("TWEET_METRIC_PURGE_MAX_ROWS", "500000"))
# Feed exports run on the control worker and land on the media volume. The row
# ceiling is what stops "All time, no filters" from being an unbounded job; the
# response says when it was hit, rather than silently returning a prefix.
EXPORT_MAX_ROWS = int(os.environ.get("EXPORT_MAX_ROWS", "100000"))
# Long enough to download something you asked for and got distracted from,
# short enough that the volume does not accumulate extracts nobody wants.
EXPORT_TTL_HOURS = int(os.environ.get("EXPORT_TTL_HOURS", "24"))
# Signup is open by default. New accounts are read-only (see
# tweets.permissions.IsStaffOrReadOnly); operating the fetcher and replacing the
# shared X session require staff, granted from the Django admin.
ALLOW_REGISTRATION = os.environ.get("ALLOW_REGISTRATION", "1") == "1"

# Without this, Django's DEFAULT_LOGGING routes unhandled view/admin exceptions
# only to mail_admins, which has no email backend configured here -- the
# traceback went nowhere, not even Docker logs. Console handler makes it show
# up in `docker compose logs web` like every other logger already does.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}
