# Twitter SaaS

Standalone Django + React + Postgres operator console around the canonical CLI
fetcher. Celery beat runs one global live/historical/search cycle (the same
two-pass `tdf-*` modules). The UI is **Feed**, **Accounts** (tiers/schedule),
**Cycles**, and **Search**.

This project is self-contained at runtime: Docker installs the canonical
fetcher from ``twitter_fetcher/src`` onto ``PYTHONPATH``. Local pytest adds the
same tree via ``config.settings``. The integration seam is a subprocess runner
plus Postgres-backed state, so the proven engine (two-pass order, rolling
cutoff, watermark advance, cursor rules, tx/query health) runs exactly as it
does upstream.

## Architecture

```
frontend/ (React + Vite)  ──/api──▶  web (Django + DRF)
                                       │
                                       ├─ Postgres  ← sole durable store
                                       │    tweets, users, searches, results,
                                       │    fetcher state (KeyValueState), raw pages
                                       │
                                       └─ Redis  ← Celery broker
                                            │
                                worker + beat run canonical pipelines as
                                subprocesses in an ephemeral scratch dir,
                                then ingest normalized tweets into Postgres.
```

**Why a subprocess, not in-process adapters:** the pipeline `run_v4()` /
`run_cycle()` construct their own `StorageManager` internally, so injecting a
Postgres adapter would require editing the engine. Instead each job runs the
pipeline as `python -m ...service` with `TDF_PROJECT_ROOT` pointed at a temp
dir; durable continuity (watermarks, cursors, tx/query health) is round-tripped
through the `KeyValueState` table before and after the run, and the temp dir is
deleted. See `backend/apps/fetching/runner.py`.

## Prerequisites

- Docker + Docker Compose (simplest), or
- Python 3.11, Node 18+, a local Postgres and Redis.

## Quick start (Docker)

```bash
cd twitter-saas
cp .env.example .env            # edit DJANGO_SECRET_KEY at least
docker compose up --build       # postgres, redis, web, worker, beat
```

In another shell, create an admin user and seed data:

```bash
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py seed_data          # tracked accounts + searches
docker compose exec web python manage.py load_xsession --file /path/to/session.json
```

Then run the frontend dev server (proxies `/api` to `web`):

```bash
cd frontend
npm install
npm run dev                     # http://localhost:5173
```

## Quick start (local, no Docker)

```bash
# from the repo root that holds .venv
./.venv/bin/pip install -r twitter-saas/backend/requirements.txt

cd twitter-saas/backend
export $(grep -v '^#' ../.env.example | xargs)   # or use your own .env
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_data
python manage.py load_xsession --file /path/to/session.json

python manage.py runserver                       # API + admin at :8000
celery -A config worker -l info                  # in a second shell
celery -A config beat -l info                    # in a third shell
```

## Loading the shared X session

The app uses **one** server-side X session for all users; app users never supply
X credentials. Provide it once as JSON:

```json
{
  "cookies": {"auth_token": "…", "ct0": "…"},
  "headers": {"authorization": "Bearer …", "x-csrf-token": "…"}
}
```

Load it via `python manage.py load_xsession --file session.json`
(or set `X_SESSION_JSON` and run `load_xsession` with no `--file`). Re-running
replaces the active session. Use `--deactivate-others` to force a single active
row.

## API

All endpoints require token auth (`Authorization: Token <key>`), obtained from
`/api/auth/login/` or `/api/auth/register/`.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| POST | `/api/auth/register/` | create user, returns token |
| POST | `/api/auth/login/` | returns token |
| GET  | `/api/feed/` | tracked accounts + enabled searches; filters: account, tier, endpoint, type, since, until, run_id |
| GET/POST/PATCH | `/api/accounts/` | operator account list / track / update tier, tracking, quarantine |
| POST | `/api/accounts/{handle}/fetch/` | on-demand live + historical for one handle |
| GET  | `/api/accounts/{handle}/tweets/` | chronological account timeline |
| GET  | `/api/runs/` | cycle history |
| GET  | `/api/runs/{run_id}/` | run detail + redacted log excerpt |
| POST | `/api/cycles/` | queue one global live/historical/search cycle |
| GET  | `/api/searches/?product=Top\|Latest` | list searches |
| POST | `/api/searches/` | submit a query → enqueues a fetch job |
| GET  | `/api/searches/{id}/results/` | ranked results for a search |
| POST | `/api/searches/{id}/refresh/` | re-enqueue a search run |

## Background jobs

`beat` schedules three periodic tasks (intervals from `.env`). Each tick runs
**one** canonical subprocess (`tdf-live --once`, `tdf-historical`, or
`tdf-search --once`). CLI due-logic and tiers decide who is fetched.

On-demand tasks remain for a single account (`--only` / `--account`) or one
search. Tracking an account from the Accounts page queues that diagnostic path.

`FETCH_MAX_ACCOUNTS_PER_RUN` caps how many tracked accounts are written into
scratch `accounts.json`. Raise the cap when the tracked set grows.

## Tests

```bash
cd twitter-saas/backend
../../.venv/bin/pytest          # SQLite + eager Celery, fetcher mocked
```

- **Unit** — ingest dedup/upsert idempotency, `KeyValueState` round-trip.
- **Integration** — feed merge/order/dedup, follow auto-enqueue, search create.

## Layout

```
twitter-saas/
  backend/
    config/            Django project (settings, urls, celery, wsgi/asgi)
    apps/
      accounts/        auth users, Follow, SearchSubscription
      tweets/          TwitterUser, Tweet, Search, SearchResult, state models
      fetching/        subprocess runner + Celery tasks (engine via PYTHONPATH)
    seed/              accounts.json, searches.json, config.example.json
  frontend/            React + Vite (Feed / Accounts / Cycles / Search + auth)
  docker-compose.yml   local stack (published ports)
  docker-compose.prod.yml  VPS overlay (frontend :80 only)
```
