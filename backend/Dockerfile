# syntax=docker/dockerfile:1
# Build context is the repo root (see docker-compose.yml).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Runtime deps only. `build-essential` and `libpq-dev` used to be here for
# psycopg, which has shipped binary wheels for years -- so they compiled nothing,
# and left a C toolchain in a production image for no reason. `curl` stays: the
# web service's healthcheck in docker-compose.yml calls it.
#
# The rest is the Chromium runtime. `playwright install --with-deps` below would
# apt-install most of it anyway, but naming it here keeps the image buildable
# when that step is cached and makes the dependency visible.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libdbus-1-3 libxcb1 libxkbcommon0 libx11-6 libxcomposite1 libxdamage1 \
    libxext6 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Chromium for auth refresh and the deep-search hybrid pages.
RUN playwright install --with-deps chromium

# The engine ships inside the backend package tree, so there is no PYTHONPATH
# seam: workers run it as `python -m fetcher.<pipeline>`.
COPY backend /app

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
