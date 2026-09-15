# Architecture

Where things live and which way dependencies point. For how to run it, see the
README; for the conventions an agent must not break, see AGENTS.md.

## The shape of it

Two processes' worth of code, separated by a real process boundary rather than a
naming convention:

```
  browser
    │
    ▼
  frontend/          React 19 + Vite SPA, served by nginx, which also proxies
    │                /api/ and /admin/ to gunicorn. One origin.
    ▼
  backend/config/    Django project: settings, urls, celery app, middleware
    │
    ▼
  backend/tweets/    the data and the HTTP surface
    │
    ▼
  backend/fetching/  Django app: Celery tasks, the subprocess runner, ingest
    │
    │  subprocess: python -m engine.<pipeline>
    │  argv + TDF_* env in, JSON on the scratch disk out
    ▼
  backend/engine/    the X engine. Pure Python. Imports no Django.
```

The engine never touches Postgres. It writes JSON to a scratch directory that
`fetching/runner.py` creates, reads back and deletes; `fetching/ingest.py` is the
only thing that turns engine output into rows. That is why `engine/` can be run
from a shell with no database and no Django settings.

## Layers inside the Django side

Dependencies flow one way. Nothing lower imports anything higher.

| Layer | Modules | May import |
| --- | --- | --- |
| transport | `tweets/views.py`, `tweets/auth_views.py`, `tweets/analytics.py`, `tweets/serializers.py`, `tweets/admin.py`, `tweets/urls.py` | everything below |
| query / domain | `tweets/feed.py`, `tweets/stats.py`, `tweets/windows.py`, `tweets/topics.py`, `tweets/textclean.py` | models, and each other |
| orchestration | `fetching/*` | `tweets/` models + query layer, `engine/` |
| data | `tweets/models.py`, migrations | nothing in this project |
| engine | `engine/*` | nothing in this project |

Transport modules parse a request, check permission, call down, and serialize.
They hold no SQL: `grep -c 'connection.cursor()' backend/tweets/analytics.py` is
0, against 5 in `backend/tweets/stats.py`.

## The four rules

**1. `engine/` imports nothing from Django, `tweets/` or `fetching/`.**
It is a library that happens to live in this repo. The moment it imports
`django.conf.settings`, it stops being runnable as `python -m engine.live` and
the process boundary becomes a lie. Configuration reaches it as `TDF_*` env vars
set by `fetching/runner.py`, and only there.

**2. Nothing in `tweets/` imports `fetching/` from inside a function.**
A function-local import in this codebase means a circular dependency is being
hidden. There were 35 of them; they are gone. If a top-level
`from fetching.x import y` in `tweets/` raises ImportError, the fix is to move
the shared thing down into `tweets/feed.py`, `tweets/stats.py` or
`tweets/windows.py` — not to indent the import.

**3. Celery task names and the `fetching` module path are wire identifiers.**
`fetching.tasks.*` is written into `config/celery.py`'s `task_routes` and beat
schedule, and into every job already sitting on Redis. Renaming a task or the
app strands queued work. `scripts/deploy_vps.sh` fails the deploy if anything
lands on the legacy `celery` queue, which is what that check is for.

**4. A request value never reaches a SQL string.**
Bucket granularity is looked up in `windows.BUCKET_KINDS`; handles are bound
parameters. The f-strings in `tweets/stats.py` interpolate module constants
only. `tests/test_raw_sql_placeholders.py` enforces the related rule that
percent signs stay out of the narratives query entirely, comments included —
the driver scans the whole string for placeholders.

## Things that look wrong and are not

- **`Tweet` and `SearchTweet` are two tables for one shape.** Deliberate. Two
  collectors with different cadence, retention and meaning; see "Two collectors,
  two tables" in the README.
- **`ROOT_LOGGER_NAME` is `"fetcher"` inside `engine/`.** It is a runtime logger
  namespace that predates the package rename and appears in every log line on
  the VPS. Renaming it would change output operators grep.
- **The raw-SQL analytics paths are skipped on SQLite.** They check
  `connection.vendor` and return early, so a local `pytest` with no
  `TEST_POSTGRES_HOST` proves nothing about them. CI runs Postgres 16. So should
  you, before trusting a green run:
  ```
  TEST_POSTGRES_HOST=127.0.0.1 python -m pytest -q
  ```

## Frontend

```
frontend/src/
  main.jsx  App.jsx  index.css     entry, route table, design tokens
  pages/                           one per route
  layouts/                         AuthLayout
  components/                      shared components, incl. filters
  ui/                              design-system primitives (Radix + CVA)
  hooks/                           usePoll, useMediaQuery
  lib/                             api, format, charts, cn
  context/                         auth
```

Import via the `@/` alias, never a relative path that climbs (`../../`). There is
no state library and no data-fetching library: `lib/api.js` is a `fetch` wrapper
that handles JWT refresh, and pages hold their own `useState`.
