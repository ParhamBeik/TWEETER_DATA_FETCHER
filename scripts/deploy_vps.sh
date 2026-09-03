#!/usr/bin/env bash
# Build/restart step of the pull-based deploy. Never edit files in this
# checkout directly -- it exists only to track what's deployed; make changes
# on your dev machine, commit, push, then redeploy.
#
# Not the thing that updates the checkout -- that's the tiny wrapper on the
# VPS (outside this repo, so `git reset --hard` mid-deploy can never rewrite
# a script bash is still reading): it fetches, resets to origin/main, *then*
# calls this script, which only ever runs as a complete, already-on-disk
# file. Keep it that way; don't merge the fetch/reset into this file. See
# /opt/apps/deploy_twitter.sh on the VPS.
set -euo pipefail
cd "$(dirname "$0")/.."

# The compose files moved from twitter-saas/ to the repo root. .env is
# gitignored, so a pull leaves the old one behind where compose no longer
# looks; carry it over once rather than booting without secrets.
if [ ! -f .env ] && [ -f twitter-saas/.env ]; then
  echo "migrating twitter-saas/.env -> ./.env"
  mv twitter-saas/.env .env
fi

if [ ! -f .env ]; then
  echo "FATAL: no .env at $(pwd). Copy .env.example and fill it in." >&2
  exit 1
fi

# Every compose call must carry the production overlay. Bare `docker compose`
# silently used the base file alone, so docker-compose.prod.yml was dead config
# for the whole life of the deployment: Postgres (:5434), Redis (:6380) and
# gunicorn (:8002) were published on the public interface that the overlay's
# `ports: !reset []` exists to close, signup stayed open, and Postgres ran on
# the base 512m instead of 1g. An overlay that is only applied when someone
# remembers the flag is not applied.
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)

"${COMPOSE[@]}" build

# Prove the app actually came back before reporting success. A build that
# succeeds and a container that boot-loops look identical to a bare `up -d`,
# and CI calls this script -- a green deploy job that left the site down is
# worse than a red one. `--wait` blocks on the healthchecks already declared in
# docker-compose.yml (gunicorn, all four celery containers, postgres, redis)
# rather than reimplementing them here.
if ! "${COMPOSE[@]}" up -d --remove-orphans --wait --wait-timeout 300; then
  echo "FATAL: services did not become healthy" >&2
  "${COMPOSE[@]}" ps
  "${COMPOSE[@]}" logs --tail 50 web >&2
  exit 1
fi

# "Backend up" is not "site up": the frontend is the one service with no
# healthcheck of its own, so `--wait` only proves its container started. An
# nginx that lost its proxy config would still leave the deploy green while
# nobody could reach the app. Ask the thing users actually hit.
echo "waiting for the frontend to serve..."
for attempt in $(seq 1 20); do
  if "${COMPOSE[@]}" exec -T frontend wget -q -O /dev/null http://127.0.0.1/ 2>/dev/null; then
    echo "frontend healthy after ${attempt} attempt(s)"
    break
  fi
  if [ "$attempt" -eq 20 ]; then
    echo "FATAL: frontend did not serve after 20 attempts" >&2
    "${COMPOSE[@]}" ps
    "${COMPOSE[@]}" logs --tail 50 frontend >&2
    exit 1
  fi
  sleep 3
done

docker image prune -f
