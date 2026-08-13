#!/usr/bin/env bash
# Nightly Postgres backup for twitter-saas. Run from host cron or a sidecar.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${BACKUP_DIR:-$ROOT/backups}"
mkdir -p "$OUT_DIR"
FILE="$OUT_DIR/twitter_saas_${STAMP}.sql.gz"

docker compose exec -T postgres \
  pg_dump -U "${POSTGRES_USER:-postgres}" "${POSTGRES_DB:-twitter_saas}" \
  | gzip -c > "$FILE"

# Keep last 14 dumps.
ls -1t "$OUT_DIR"/twitter_saas_*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
echo "wrote $FILE"
