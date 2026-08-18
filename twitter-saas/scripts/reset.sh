#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
docker compose down -v
docker compose build
docker compose up -d --force-recreate
docker compose exec web python manage.py migrate --noinput
docker compose exec web python manage.py seed_data
