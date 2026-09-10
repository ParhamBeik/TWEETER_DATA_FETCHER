#!/usr/bin/env bash
# Prove a dump written by backup_pg.sh is complete, without restoring it onto
# anything. Restore stays a separate, explicit host operation (see README).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FILE="${1:-}"

if [ -z "$FILE" ]; then
  DIR="${BACKUP_DIR:-$ROOT/backups}"
  FILE="$(ls -1t "$DIR"/twitter_saas_*.sql.gz 2>/dev/null | head -1 || true)"
fi

if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
  echo "FATAL: no dump to check. Pass a path or put files in backups/" >&2
  exit 1
fi

if ! gzip -t "$FILE"; then
  echo "FATAL: $FILE is not a valid gzip" >&2
  exit 1
fi

if ! gunzip -c "$FILE" | tail -5 | grep -q "PostgreSQL database dump complete"; then
  echo "FATAL: $FILE does not end with pg_dump's completion marker" >&2
  exit 1
fi

echo "ok $FILE ($(du -h "$FILE" | cut -f1))"
