#!/usr/bin/env bash
# Put a previous build back into service, without rebuilding anything.
#
#   ./scripts/rollback_vps.sh              # list what you can roll back to
#   ./scripts/rollback_vps.sh 4d3e24d      # restore that build
#
# Run it on the VPS, from /opt/apps/twitter-project. It restarts containers from
# images already on disk, so it takes seconds rather than the minutes a revert
# commit plus a full CI rebuild costs.
#
# THIS DOES NOT ROLL BACK THE DATABASE. Migrations run forward on every deploy
# and are never reversed here. If the deploy you are undoing added a migration,
# the old code will be running against a newer schema. Django tolerates that for
# additive changes (a new nullable column the old code ignores) and does not for
# anything else. Check what the bad deploy migrated before trusting this.
#
# It is also a temporary state: the next manually dispatched deployment
# overwrites this. Use the time it buys to fix the commit on main.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
BUILT_IMAGES=(twitter-saas-web twitter-saas-beat twitter-saas-frontend
              twitter-saas-worker_live twitter-saas-worker_historical
              twitter-saas-worker_search twitter-saas-worker_control)

available() {
  docker images twitter-saas-web --format '{{.Tag}} {{.CreatedAt}}' | grep -v '^latest '
}

if [ $# -lt 1 ]; then
  echo "Builds you can roll back to:"
  echo
  available | sort -k2 -r | while read -r tag rest; do
    printf '  %-12s %s\n' "$tag" "$rest"
  done
  echo
  echo "Usage: $0 <tag>"
  exit 0
fi

TARGET="$1"

# Refuse before touching anything if any one service is missing that tag --
# a partial rollback (new web against old workers) is worse than none.
missing=()
for image in "${BUILT_IMAGES[@]}"; do
  docker image inspect "$image:$TARGET" >/dev/null 2>&1 || missing+=("$image:$TARGET")
done
if [ ${#missing[@]} -ne 0 ]; then
  echo "FATAL: no such build for: ${missing[*]}" >&2
  echo >&2
  echo "Available:" >&2
  available | sort -k2 -r | awk '{print "  " $1}' >&2
  exit 1
fi

echo "rolling back to $TARGET"
for image in "${BUILT_IMAGES[@]}"; do
  docker tag "$image:$TARGET" "$image:latest"
done

# No --build: the whole point is to use the images already on disk.
if ! "${COMPOSE[@]}" up -d --remove-orphans --wait --wait-timeout 300; then
  echo "FATAL: services did not become healthy after rollback" >&2
  "${COMPOSE[@]}" ps
  "${COMPOSE[@]}" logs --tail 50 web >&2
  exit 1
fi

echo "waiting for the frontend to serve..."
for attempt in $(seq 1 20); do
  if "${COMPOSE[@]}" exec -T frontend wget -q -O /dev/null http://127.0.0.1/ 2>/dev/null; then
    echo "frontend healthy after ${attempt} attempt(s)"
    break
  fi
  if [ "$attempt" -eq 20 ]; then
    echo "FATAL: frontend did not serve after rollback" >&2
    "${COMPOSE[@]}" logs --tail 50 frontend >&2
    exit 1
  fi
  sleep 3
done

echo "$TARGET" > .deployed_sha
echo
echo "rolled back to $TARGET and healthy."
echo "This is temporary: the next manual deployment will replace it."
echo "Fix the bad commit on main before the next deployment."
