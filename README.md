# Twitter Data Fetcher

Collects tweets from X for a tracked set of accounts and saved searches, stores
them in Postgres, and serves them through a React operator console.

```
frontend/ (React)  ──/api──▶  Django + DRF  ──▶  Postgres   (the only durable store)
                                   │
                                   └──▶  Redis  ──▶  3 Celery workers
                                                      │
                                          each runs the X engine as a
                                          subprocess in a temp dir, then
                                          ingests the results into Postgres
```

## Layout

```
backend/
  config/      Django project: settings, urls, celery
  tweets/      models, API views, serializers, analytics, admin
  fetching/    Celery tasks + the runner that drives the engine
  fetcher/     the X engine (HTTP, pagination, auth, parsing, storage)
  tests/       one suite covering both the engine and the API
frontend/      React + Vite SPA
scripts/       deploy and backup
```

## Run it

```bash
cp .env.example .env          # set DJANGO_SECRET_KEY (32+ chars) at minimum
docker compose up --build     # postgres, redis, web, 3 workers, beat, frontend
```

Then, in another shell:

```bash
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py seed_data       # tracked accounts + searches
docker compose exec web python manage.py load_xsession --file session.json
```

The console is at http://localhost:8080, the Django admin at
http://localhost:8002/admin/.

### Without Docker

```bash
pip install -r backend/requirements.txt
cd backend
python manage.py migrate && python manage.py runserver
celery -A config worker -l info -Q live,historical,search   # second shell
celery -A config beat -l info                               # third shell
cd ../frontend && npm install && npm run dev                # http://localhost:5173
```

## The X session

One server-side X session serves every user; app users never supply X
credentials. Provide it once:

```json
{
  "cookies": {"auth_token": "…", "ct0": "…"},
  "headers": {"authorization": "Bearer …", "x-csrf-token": "…"}
}
```

`python manage.py load_xsession --file session.json` (or set `X_SESSION_JSON`).
Re-running replaces the active session. The Session page in the console accepts
the same payload, or a whole exported engine `config.json`. Both are *staff*-only.

## API

JWT auth (`Authorization: Bearer <access>`) from `/api/auth/login/` or
`/register/`. Access tokens last 30 minutes; the frontend refreshes them
transparently. Signup is open, and new accounts are **read-only** -- the
operator endpoints below (marked *staff*) need `is_staff`, granted in
`/admin/`.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/auth/register/` \| `/login/` | returns `{access, refresh, user}` |
| POST | `/api/auth/refresh/` | rotates the pair; the spent refresh token is blacklisted |
| POST | `/api/auth/logout/` | blacklists the refresh token |
| GET | `/api/auth/me/` | current identity, including `is_staff` |
| GET | `/api/feed/` | tracked accounts + enabled searches; filters: `account`, `tier`, `since`, `until`, `run_id`, `q` |
| GET | `/api/export/?format=jsonl\|csv` | stream the current feed |
| GET/POST/PATCH | `/api/accounts/` | read for all; write is *staff* (track, set tier, clear quarantine) |
| POST | `/api/accounts/{handle}/fetch/` | *staff* — on-demand live + historical for one handle |
| GET | `/api/accounts/{handle}/tweets/` | one account's timeline |
| GET | `/api/runs/` \| `/api/runs/{run_id}/` | cycle history and detail |
| POST | `/api/cycles/` | *staff* — queue one global cycle (`live`, `historical`, `search`) |
| GET/POST | `/api/searches/` | read for all; create is *staff* (and enqueues a fetch) |
| GET | `/api/searches/{id}/results/` | ranked results |
| GET | `/api/stats/overview/`, `/api/analytics/{velocity,topics,accounts,narratives}/` | dashboard data |

## Scheduling

Beat ticks three periodic tasks, each on its own queue and worker so a
rate-limit sleep in one cannot block the others:

| Task | Default interval | Env var |
| --- | --- | --- |
| live poll (all due accounts) | 30 min | `FETCH_LIVE_INTERVAL_SECONDS` |
| historical archive walk (1 account/tick) | 5 min | `FETCH_HISTORICAL_INTERVAL_SECONDS` |
| search dispatch (queues whoever is due) | 5 min | `FETCH_SEARCH_DISPATCH_SECONDS` |
| recompute poll intervals | daily | — |

All three fetchers spend **one** X rate budget (`UserTweets` 50 per 15 min),
so the split between them is explicit:

- **Live** keeps the last few hours current. It polls each account on its own
  cadence, measured from how often that account really posts and clamped into
  the band its priority tier allows, and never paginates deeper than 3 pages.
- **The archive walk** is a finite backward pass per account. It resumes from
  its own stored cursor each tick, stops after
  `FETCH_HISTORICAL_PAGES_PER_TICK` pages, always leaves
  `FETCH_HISTORICAL_QUOTA_FLOOR` requests for live, and leaves the queue for
  good once it reaches the end of an account's timeline.
- **Search** runs one query per task on its own `Search.interval_seconds`, so
  no query can be starved by the ones ahead of it. Deep pages come from browser
  scrolling, and a repoll stops once it reaches tweets the last run stored.

Search-only tweets are purged after 30 days, run records after 90.

## Tests

```bash
cd backend  && python -m pytest -q    # engine + API, SQLite, no services needed
cd frontend && npm test               # component suite
```

## Deploying

A push to `main` deploys automatically: GitHub Actions runs both test suites,
then SSHes to the VPS as the unprivileged `deploy` user and runs the wrapper.
See `.github/workflows/ci.yml`; it needs the `VPS_HOST`, `VPS_USER`,
`VPS_SSH_KEY` and `VPS_KNOWN_HOSTS` secrets.

`scripts/deploy_vps.sh` builds, restarts, and then polls the API until it
answers -- a deploy that leaves the site down fails the job. It is called by a
small wrapper on the VPS that does the `git fetch`/`reset` first, so a deploy
can never rewrite a script bash is still reading.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

`scripts/backup_pg.sh` writes a nightly gzipped `pg_dump` and keeps the last 14.
