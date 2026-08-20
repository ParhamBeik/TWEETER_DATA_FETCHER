# syntax=docker/dockerfile:1
# Build context must be the git repo root (see docker-compose.yml).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/fetcher

WORKDIR /app

# System deps for psycopg + curl_cffi + playwright runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libdbus-1-3 libxcb1 libxkbcommon0 libx11-6 libxcomposite1 libxdamage1 \
    libxext6 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

COPY twitter-saas/backend/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Chromium for auth refresh / optional deep-search hybrid pages.
RUN playwright install --with-deps chromium

COPY twitter_fetcher/src /app/fetcher
COPY twitter-saas/backend /app/backend

WORKDIR /app/backend

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
