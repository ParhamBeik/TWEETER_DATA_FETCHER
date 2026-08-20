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
the same payload, or a whole exported engine `config.json`.

## API

Token auth (`Authorization: Token <key>`) from `/api/auth/login/`.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/auth/login/` \| `/register/` | returns a token |
| GET | `/api/feed/` | tracked accounts + enabled searches; filters: `account`, `tier`, `since`, `until`, `run_id`, `q` |
| GET | `/api/export/?format=jsonl\|csv` | stream the current feed |
| GET/POST/PATCH | `/api/accounts/` | track accounts, set tier, clear quarantine |
| POST | `/api/accounts/{handle}/fetch/` | on-demand live + historical for one handle |
| GET | `/api/accounts/{handle}/tweets/` | one account's timeline |
| GET | `/api/runs/` \| `/api/runs/{run_id}/` | cycle history and detail |
| POST | `/api/cycles/` | queue one global cycle (`live`, `historical`, `search`) |
| GET/POST | `/api/searches/` | list / create (creating enqueues a fetch) |
| GET | `/api/searches/{id}/results/` | ranked results |
| GET | `/api/stats/overview/`, `/api/analytics/{velocity,topics,accounts,narratives}/` | dashboard data |

## Scheduling

Beat ticks three periodic tasks, each on its own queue and worker so a
rate-limit sleep in one cannot block the others:

| Task | Default interval | Env var |
| --- | --- | --- |
| live poll (all due accounts) | 30 min | `FETCH_LIVE_INTERVAL_SECONDS` |
| historical backfill (1 account/tick, oldest first) | 5 min | `FETCH_HISTORICAL_INTERVAL_SECONDS` |
| search repoll (enabled searches) | 30 min | `FETCH_SEARCH_INTERVAL_SECONDS` |

Search-only tweets are purged after 30 days, run records after 90.

## Tests

```bash
cd backend  && python -m pytest -q    # engine + API, SQLite, no services needed
cd frontend && npm test               # component suite
```

## Deploying

`scripts/deploy_vps.sh` builds and restarts the stack. It is called by a small
wrapper on the VPS that does the `git fetch`/`reset` first, so a deploy can
never rewrite a script bash is still reading.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

`scripts/backup_pg.sh` writes a nightly gzipped `pg_dump` and keeps the last 14.
