# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for psycopg + curl_cffi + playwright runtime.
# The extra libs (libnss3, libgbm1, ...) are the shared libs playwright's
# chromium binary links against at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libdbus-1-3 libxcb1 libxkbcommon0 libx11-6 libxcomposite1 libxdamage1 \
    libxext6 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Install the chromium browser binary for playwright. The vendored fetcher's
# BrowserBootstrap (x_api/browser.py) is invoked as the fallback path when the
# X GraphQL API returns a deep-page 404 anti-abuse gate — the worker needs a
# real browser to refresh tx-id/query params. `available()` degrades gracefully
# (returns False) if this is skipped, but then the browser fallback is dead.
# selenium is listed in requirements but NOT imported anywhere in the codebase;
# it rides along as an unused spec — no browser install for it.
RUN playwright install --with-deps chromium

COPY backend /app/backend

WORKDIR /app/backend

# collectstatic is run from the compose command (guarded) so it does not abort
# startup if STATIC_ROOT is unset or DEBUG=1.
EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
