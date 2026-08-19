#!/usr/bin/env bash
# Pull-based deploy: run this ON THE VPS to bring the running containers up
# to date with origin/main. Never edit files in this checkout directly —
# it exists only to track what's deployed; make changes on your dev machine,
# commit, push, then run this.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Refusing to deploy: this checkout has local modifications." >&2
  echo "This directory is a deploy target, not a place to edit code." >&2
  git status --short >&2
  exit 1
fi

git fetch origin main
git reset --hard origin/main

cd twitter-saas
docker compose build
docker compose up -d
docker image prune -f
