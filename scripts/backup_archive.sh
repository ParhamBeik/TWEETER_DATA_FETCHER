#!/usr/bin/env bash
# Nightly archive snapshot: verified PostgreSQL dump plus the media volume.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
source scripts/compose_config.sh
exec 8>/var/lock/twitter-saas-archive-backup.lock
flock -n 8 || exit 0
OUT_DIR="${BACKUP_DIR:-$PWD/backups}"
mkdir -p "$OUT_DIR"
BACKUP_DIR="$OUT_DIR" bash scripts/backup_pg.sh
DUMP=$(ls -1t "$OUT_DIR"/twitter_saas_*.sql.gz | head -1)
MEDIA="${DUMP%.sql.gz}.media.tar.gz"
trap 'rm -f "$MEDIA.part"' EXIT
"${COMPOSE[@]}" exec -T web tar -czf - -C /app/media . > "$MEDIA.part"
tar -tzf "$MEDIA.part" >/dev/null
mv "$MEDIA.part" "$MEDIA"
(cd "$OUT_DIR" && sha256sum "$(basename "$DUMP")" "$(basename "$MEDIA")" > "$(basename "${DUMP%.sql.gz}").sha256")
# Rotate media and manifests only after a complete pair is available.
for old in "$OUT_DIR"/twitter_saas_*.media.tar.gz; do
  [ -e "${old%.media.tar.gz}.sql.gz" ] || rm -f "$old" "${old%.media.tar.gz}.sha256"
done
date -u +%FT%TZ > "$OUT_DIR/last-success.txt"
echo "database and media backup verified"
