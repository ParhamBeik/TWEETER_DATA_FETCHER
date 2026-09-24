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

# --- Rollback artifacts -----------------------------------------------------
#
# `docker image prune -f` at the end of this script used to delete the previous
# build the moment a new one replaced it, because rebuilding leaves the old
# `:latest` dangling. That left no way back from a bad deploy except a revert
# commit and a full rebuild -- minutes of downtime for a one-line mistake.
#
# Images are now tagged with the commit that produced them, which makes them
# non-dangling, so prune leaves them alone. ROLLBACK_KEEP of them are retained.
# A tag is a pointer, not a copy: the layers are already on disk and shared, so
# five tags cost approximately nothing beyond the layers that differ.
BUILT_IMAGES=(twitter-saas-web twitter-saas-beat twitter-saas-frontend
              twitter-saas-worker_live twitter-saas-worker_historical
              twitter-saas-worker_search twitter-saas-worker_control)
ROLLBACK_KEEP=5
SHA_FILE=.deployed_sha
NEW_SHA=$(git rev-parse --short HEAD)

# Tag what is running *now*, before the build replaces it. Without this the
# currently-live build is dangling the instant the new one is built, and the
# version you would most want to return to is the one that gets deleted.
if [ -f "$SHA_FILE" ]; then
  PREV_SHA=$(cat "$SHA_FILE")
  for image in "${BUILT_IMAGES[@]}"; do
    if docker image inspect "$image:latest" >/dev/null 2>&1; then
      docker tag "$image:latest" "$image:$PREV_SHA" 2>/dev/null || true
    fi
  done
  echo "preserved the running build as :$PREV_SHA"
fi

"${COMPOSE[@]}" build

# Prove the app actually came back before reporting success. A build that
# succeeds and a container that boot-loops look identical to a bare `up -d`.
# `--wait` blocks on the healthchecks already declared in
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

# Current tasks are routed to named queues. Anything in Celery's default queue
# is an unroutable legacy message and must make the deploy visibly fail rather
# than sitting unnoticed forever.
default_queue=$("${COMPOSE[@]}" exec -T redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --no-auth-warning LLEN celery')
if [ "$default_queue" -ne 0 ]; then
  echo "FATAL: default Celery queue contains $default_queue legacy message(s)" >&2
  exit 1
fi

"${COMPOSE[@]}" exec -T web python manage.py fetch_report --since 24h

# Only now, past every health gate, is this build worth keeping. Tagging here
# rather than straight after `build` means a build that never became healthy is
# not offered as a rollback target.
for image in "${BUILT_IMAGES[@]}"; do
  docker tag "$image:latest" "$image:$NEW_SHA" 2>/dev/null || true
done
echo "$NEW_SHA" > "$SHA_FILE"
echo "tagged this build as :$NEW_SHA"

# Drop the oldest rollback tags, newest ROLLBACK_KEEP retained. Sorted by image
# creation time, so the order follows when each build happened rather than the
# alphabetical accident of its sha.
for image in "${BUILT_IMAGES[@]}"; do
  docker images "$image" --format '{{.Tag}} {{.CreatedAt}}' \
    | grep -v '^latest ' \
    | sort -k2 -r \
    | tail -n +$((ROLLBACK_KEEP + 1)) \
    | awk '{print $1}' \
    | while read -r old; do
        echo "  retiring $image:$old"
        docker rmi "$image:$old" >/dev/null 2>&1 || true
      done
done

# Safe now: every build worth keeping carries a tag, so nothing here is
# dangling. This only reclaims intermediate layers no tag points at.
docker image prune -f

echo
echo "rollback targets available:"
docker images twitter-saas-web --format '  {{.Tag}}  ({{.CreatedAt}})' | grep -v '^  latest'
echo "  roll back with: ./scripts/rollback_vps.sh <tag>"
