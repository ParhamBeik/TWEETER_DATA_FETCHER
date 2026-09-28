# Archive deployment on the Iran VPS

The September 28, 2026 deployment uses a fresh database by the operator's choice.
No old tweets or collection checkpoints were recovered. This deployment supersedes
the full-collection host rejection in production-recovery.md only for paused use.

## Deploy and operate

Run `bash scripts/deploy_vps.sh` from `/opt/apps/twitter-project`.
Archive mode is the default for deployment, rollback and backups. It combines
base, production and archive Compose files, starts only postgres, redis, web,
frontend and worker_control, and explicitly stops the three collectors and beat.
The archive overlay forces INGESTION_ENABLED=0 even if .env says otherwise.
Do not operate this host with bare `docker compose up`.

`bash scripts/rollback_vps.sh <tag>` uses the same mode. It switches images only;
it never reverses database migrations. Archive builds omit Chromium. Full
collection needs a new build, not just starting an old archive image.

All stored-data pages and asynchronous exports work without X. Account/search
configuration is saved, but collection actions return 409 with
`code=ingestion_disabled`. Search schedules report paused without changing the
saved per-search enabled flag. Background fetch, media and archive-retention
tasks return without changing data; beat registers no schedules in paused mode.
The runner also refuses direct collection calls. Enabling ingestion requires a
process restart; this is an operator setting, not a UI toggle.

## Backups

Host cron runs `bash scripts/backup_archive.sh` daily. It locks, writes a verified
PostgreSQL dump, archives media, and publishes a checksum manifest only after both
complete. Files are private. Keep 14 snapshots. Exports live in the media volume;
cron separately runs the existing expired-export cleanup to bound their growth.

On the Mac, `bash scripts/pull_archive_backups.sh` copies backups over SSH into
`~/Library/Application Support/TwitterArchive/backups`, checks the latest pair,
and records `last-pull-success.txt` and `last-verified-snapshot.txt`. A LaunchAgent
runs this daily and at login when the Mac is available. An offline Mac misses
copies; inspect those timestamps rather than assuming daily off-server coverage.
Copy time and snapshot time are different: inspect the filename in the latter.

Verify restore in an isolated database and media directory. Never restore a
verification snapshot over the application's database. Database and media are
sequential snapshots; for a coordinated migration, stop writers before the final
pair and cutover.

## Enable collection later

First prove worker-network HTTPS, authenticated X requests, Playwright navigation
and a media download via an approved egress endpoint. Existing HTTPS_PROXY support
accepts an HTTP CONNECT proxy, with separate Playwright handling. Proxy configuration
alone does not establish connectivity. Confirm RAM/disk capacity before starting
browser workers. Set INGESTION_ENABLED=1 in the private environment and deploy with
`TDF_DEPLOY_MODE=collection bash scripts/deploy_vps.sh` only after those checks.
Start with one bounded run. Resumption uses stored checkpoints; it cannot recover
lost historical data or guarantee every missed tweet.

For new hardware, migrate a verified PostgreSQL/media snapshot, preserve the
`twitter-saas` project/volume names, restore initially in archive mode, and repeat
the checks before enabling collection. Keep API permissions and X session handling
unchanged. Review retention before enabling beat against an old archive.
