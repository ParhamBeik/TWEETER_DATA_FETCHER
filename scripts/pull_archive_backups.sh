#!/usr/bin/env bash
# Run on the Mac. No --delete: an empty remote directory cannot erase copies.
set -euo pipefail
umask 077
DEST="${TDF_BACKUP_DEST:-$HOME/Library/Application Support/TwitterArchive/backups}"
mkdir -p "$DEST"
rsync -az -e 'ssh -o BatchMode=yes -o ConnectTimeout=15' \
  --include='twitter_saas_*.sql.gz' --include='twitter_saas_*.media.tar.gz' \
  --include='twitter_saas_*.sha256' --exclude='*' \
  root@45.139.10.12:/opt/apps/twitter-project/backups/ "$DEST/"
cd "$DEST"
LATEST=$(ls -1t twitter_saas_*.sha256 | head -1)
shasum -a 256 -c "$LATEST"
gzip -t "${LATEST%.sha256}.sql.gz"
tar -tzf "${LATEST%.sha256}.media.tar.gz" >/dev/null
date -u +%FT%TZ > last-pull-success.txt
printf '%s\n' "$LATEST" > last-verified-snapshot.txt
# Keep 14 verified snapshots; never rotate on an unsuccessful pull.
ls -1t twitter_saas_*.sha256 | tail -n +15 | while read -r old; do
  rm -f "$old" "${old%.sha256}.sql.gz" "${old%.sha256}.media.tar.gz"
done
