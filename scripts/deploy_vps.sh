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

docker compose build
docker compose up -d --remove-orphans
docker image prune -f
