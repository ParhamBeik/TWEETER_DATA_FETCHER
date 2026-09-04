#!/usr/bin/env bash
# Nightly Postgres backup for twitter-saas. Run from host cron or a sidecar.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${BACKUP_DIR:-$ROOT/backups}"
mkdir -p "$OUT_DIR"
FILE="$OUT_DIR/twitter_saas_${STAMP}.sql.gz"

# Written under a temporary name and moved into place only once the dump is
# proven complete.
#
# Redirecting straight into $FILE created the file *before* pg_dump ran, so a
# failed dump -- container down, wrong credentials, disk full -- left a 20-byte
# gzip holding zero bytes of SQL. That file is structurally valid, so `gunzip -t`
# reports it healthy; it sorts to the front as the newest backup; and it takes a
# slot in the 14-dump rotation below. Fourteen bad nights in a row and every
# backup that still worked had been deleted, with nothing anywhere saying so.
PARTIAL="$FILE.part"
trap 'rm -f "$PARTIAL"' EXIT

docker compose exec -T postgres \
  pg_dump -U "${POSTGRES_USER:-postgres}" "${POSTGRES_DB:-twitter_saas}" \
  | gzip -c > "$PARTIAL"

# pg_dump emits this as its final line. Checking for the marker is what tells a
# complete dump apart from one that died partway through leaving a plausible
# prefix -- a size threshold cannot, because a truncated dump of this database
# is still large. `set -o pipefail` above already catches pg_dump's exit code;
# this catches the case where it exits 0 having written less than everything.
if ! gunzip -c "$PARTIAL" | tail -5 | grep -q "PostgreSQL database dump complete"; then
  echo "FATAL: dump did not complete; previous backups left untouched" >&2
  exit 1
fi

mv "$PARTIAL" "$FILE"
trap - EXIT

# Keep the last 14 dumps. Reached only after a good one is on disk, so a run of
# failures can no longer rotate away the backups that still restore.
ls -1t "$OUT_DIR"/twitter_saas_*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
echo "wrote $FILE ($(du -h "$FILE" | cut -f1))"
