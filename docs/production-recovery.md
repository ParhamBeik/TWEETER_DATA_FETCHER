# Production recovery

The replacement VPS is not a deployment target for TDF. This records its
read-only findings and the gates for any future host; local tests are not
evidence that the application is running there.

## Known state (read-only inspection, September 24, 2026)

- The replacement host `45.139.10.12` has no `/opt/apps/twitter-project`, TDF
  containers, or TDF Caddy route. The external wrapper exists but starts by
  entering that missing checkout. GitHub's `VPS_*` secret metadata was last
  updated August 24; values were not read, so their target is unverified.
- The shared host has 7.8 GiB RAM, about 1.8 GiB available, 2.5 GiB swap in
  use, and 7.8 GiB free disk (92% used). Other applications occupy it. The TDF
  production overlay sets about 6.6 GiB of container memory limits and was
  written for a dedicated ~16 GiB host. Limits are ceilings, not a prediction
  of actual demand; measure headroom before assigning capacity.
- No TDF database or backup was found under `/opt` or the inspected
  `/var/backups` area. There is no proven source for the required archive.
  The retired host's browser observations and September 15 CI deploy success
  do not establish current runtime health. A repeat SSH banner and HTTPS probe
  of the former host timed out September 24; no matching dump or media archive
  was found in the inspected local Downloads, Documents, or mounted volumes.
- X egress fails from both the host and a disposable container on Docker's
  bridge. On September 24, `x.com`, `api.x.com`, and `twitter.com` resolved to
  private `10.10.34.x` addresses and timed out or reset; `api.twitter.com`
  failed TLS. Forcing `x.com` to a public address still failed TLS with
  `wrong version number`. A control HTTPS request to `example.com` succeeded.
  The engine uses `https://x.com/` for both GraphQL and Playwright, so direct
  collection cannot work on this route.
- The bridge container also failed TLS to `pbs.twimg.com`, `video.twimg.com`,
  and `abs.twimg.com`, so new media archiving would fail. Public DNS currently
  resolves `twitter.parhambm.ir` and `tdf.parhambm.ir` to the VPS, but the
  installed certificate lists neither name and Caddy has no TDF route. A
  direct trusted-HTTPS probe of `twitter.parhambm.ir` failed during TLS with
  `internal error`. A trusted certificate and route still need to be established.
- No off-host TDF backup destination is configured. Another application's
  script mentions `rclone`, but the current host has no `rclone` executable or
  configuration at the inspected standard locations; that script is not TDF
  backup coverage.

## Gates before a first deployment

1. Identify the former database and media archive, and confirm ownership and
   provenance without printing session credentials. Restore a copy into an
   isolated database, check migrations and counts of tracked tweets, searches,
   endpoint state, and media, and compare them with source records. An empty
   database is not an accepted substitute. If the source cannot be recovered,
   stop and decide explicitly what data loss is acceptable.
2. Inventory memory, swap, disk, Docker images/volumes, and other applications
   on the chosen host. Account for build, restore, browser worker,
   backup, and rollback-image peaks. Arrange sufficient capacity without
   deleting another application's data or images to make room. Measure the
   restored database's size and representative feed/analytics query plans.
3. Prepare a clean checkout on the approved host and verify the chosen deployment
   path before rendering the merged Compose config with both YAML files. CI has
   no deploy job. Keep the `twitter-saas` project name and existing volume
   identity if restoring that volume. Verify the selected edge route, hostname,
   DNS, and TLS from the operator's network.
   Establish an approved X egress route and test HTTPS to `x.com` from the
   worker's Docker network, including a real Playwright navigation and media
   CDN download before enabling collection. A DNS override alone did not fix
   the measured TLS failure. The backend now accepts standard `HTTPS_PROXY`
   from `.env` for Requests, urllib media downloads, and both Playwright launch
   paths; this configuration is **local-only and unverified on this VPS** until
   a working proxy or tunnel endpoint is supplied. Test the browser path
   separately even when command-line HTTP succeeds. With a proxy configured,
   network exception detail and HTTP error bodies are omitted from run reports
   to prevent proxy credentials appearing in durable diagnostics.
   Do not put proxy credentials into logs or docs.
   The following was an inactive candidate for the rejected VPS, not a route to
   apply to an unspecified future host:

   ```caddyfile
   twitter.parhambm.ir {
       reverse_proxy twitter-frontend:80
   }
   ```

   This appended cleanly to the current Caddyfile under `caddy adapt` on
   September 24; it has **not** been applied. The name resolves publicly to
   the VPS, but the current certificate lacks this name. Leave Caddy's automatic
   certificate issuance enabled and verify trusted HTTPS after activation.
4. Arrange scheduled PostgreSQL and media backups to storage outside the VPS.
   `scripts/backup_pg.sh` is not scheduled by this repository and keeps only
   the last 14 local dumps. Check a fresh dump with `verify_backup.sh`, restore
   it into an isolated database, check media samples, and record the recovery
   point and restore time. Coordinate rotation of the shared X session before
   resuming collection; do not copy or print credentials in evidence.

There is no deploy job in CI. Only after these gates pass and publication is
authorized: verify the target and wire a deploy path for the approved host.
Observe the first migration and all service health checks. Check
HTTPS, auth/permissions, feed, search, analytics, media, export download,
responsive rendering, security headers, and browser console. Use
`fetch_report --since 24h` and queue/worker logs to check run outcomes and
quota use. Compare repeated p50/p95 times and query plans against the same
restored dataset; investigate a repeatable p95 increase above 20% before
calling the release healthy. Record the committed, CI, deployed, and live
verified revisions separately.

## Recovery

The deploy script retains five tagged builds. `scripts/rollback_vps.sh <tag>`
switches images only; it does **not** reverse a database migration or restore
data. Check schema compatibility before using it. Restore a verified database
and media snapshot into isolated names first, then plan the cutover. A rollback
is temporary until the bad commit is corrected on `main`. If capacity, archive,
backup, route, or post-deploy behavior remains unverified, leave the release
blocked and report the precise gap.
