# Phase 0 Recon → Simplification Plan

Historical September 2026 planning snapshot. For the current checkout and
replacement-VPS release gates, see [production recovery](docs/production-recovery.md).
Its credential-history and deployment observations must be rechecked against
current refs and infrastructure before acting.

## Context

The brief asks for dramatic simplification of a live, auto-deploying production app.
Recon says the usual yield is not here: `ruff` passes clean, there are no tracked build
artifacts, no committed `.env`, no TODOs, no `console.log`, no orphaned files, config is
already a single validated settings module, and nearly every non-obvious line carries a
rationale comment. Deleting for line count would be churn against a well-kept tree.

What *is* wrong is structural, and it is the thing the success metric actually measures —
"find any piece of logic in under 60 seconds":

1. **A real circular dependency** between the `tweets` Django app and the `fetching` app,
   papered over with 35 function-local imports. `fetching/exports.py` reaches into
   `tweets/views.py` for `feed_queryset`; `tweets/analytics.py` reaches into a *management
   command* for `parse_since`; `fetching/session.py` and `tweets/analytics.py` import each
   other at module level.
2. **`tweets/analytics.py` is 1056 lines** of DRF views + param parsing + raw analytics SQL
   in one file.
3. **`fetcher/` vs `fetching/`** — two backend packages whose names differ by three letters.
   They are genuinely different things (pure engine library vs Django app) and the boundary
   between them is clean, but nothing about the names says so.
4. **`frontend/src` has 18 modules at its root**, mixing pages, shared components, hooks and
   utilities, with a single-file `lib/` directory sitting next to them.

Outcome: same behavior, same routes, same task names — but one obvious home per concept and
a dependency graph that flows one way.

---

## Stack (ground truth)

**Backend** — Python 3.13, Django 5.2 LTS + DRF 3.18 + SimpleJWT, Celery 5.6 on Redis 8,
Postgres 16 via psycopg 3. Deps pinned `~=` in `backend/requirements.txt`. Lint: `ruff`
(correctness rules only, no formatter, no type checker). Tests: pytest + pytest-django,
`config.settings_test`.

**Frontend** — React 19, react-router-dom 7 used with the v6 `<Routes>` component API (no
data router), Vite 8, Vitest 5 + jsdom, Tailwind v4 via `@tailwindcss/vite` with tokens in an
`@theme` block, Radix primitives + CVA + `clsx`/`tailwind-merge`, Recharts 3. JavaScript
only — no TypeScript. No state or data-fetching library; `src/api.js` is a hand-rolled
`fetch` wrapper.

**Deploy** — GitHub Actions `ci.yml`: backend job (pip-audit → compileall → ruff →
`makemigrations --check` → `check --deploy --fail-level WARNING` → pytest against a real
Postgres 16 service) and frontend job (eslint → vitest → vite build). Deploy job gates on
both, only on `push` to `main`, `environment: production`, `concurrency: deploy-production`
with `cancel-in-progress: false`. It SSHes to the VPS and runs `/opt/apps/deploy_twitter.sh`
(**not in this repo**), which resets the checkout to `origin/main` and calls
`scripts/deploy_vps.sh` → `docker compose build && up -d --wait`, then probes the frontend,
asserts the legacy `celery` queue is empty, and runs `fetch_report --since 24h`.

**Process model** — 9 compose services off one backend image: `web` (gunicorn), four
single-concurrency `-P solo` workers on queues `live`/`historical`/`search`/`control`,
`beat`, `postgres`, `redis`, `frontend` (nginx, the only published port, behind host Caddy).

**Migrations** — applied automatically, inside the `web` container's start command, every
deploy. `web` has no replicas, so no concurrent-migration race; but workers and beat do not
wait for `web`, so mid-deploy a worker can run against the old schema.

**No staging. No canary. No rollback.** De-facto rollback is `git revert` + push + full CI
redeploy (~several minutes). Worse: `scripts/deploy_vps.sh:82` runs `docker image prune -f`,
so the previous image is destroyed on every deploy. **This materially raises the risk of
every batch and is why each one below is small and independently revertible.**

---

## 🔴 STOP — committed credentials in git history

`git log --all --diff-filter=A` shows these paths were added in this repository's history:

```
TWEETER DATA FETCHING 1.0/config.json
TWEETER DATA FETCHING 2.0/config.json
TWEETER DATA FETCHING 3.0/{ADVANCED SEARCH,HISTORICAL DATA,LIVE DATA}/config.json
TWEETER DATA FETCHING 3.0/ADVANCED SEARCH/search_config.json
TWEETER DATA FETCHING 4.0/shared/config/{config.json,search_config.json}
TWEETER DATA FETCHING 3.0/ADVANCED SEARCH/monitor_search_timeline_exact_replay.py.bak
```

The repo's own `.gitignore` documents what `config.json` holds: *"`auth_token`, which is a
full login to the shared X account."* These files are gone from `HEAD` but remain reachable
in history. Also note the history contains a fully committed `frontend/node_modules`.

Per §0.8 I have **not** read the values and will **not** push a commit that merely deletes
them — deletion does not remove history. This needs, as a separate human-approved operation:
**rotate the X session credential first**, then decide on history surgery (`git filter-repo`
/ BFG + a coordinated force-push, which §0.7 forbids me from doing unilaterally) or accept
and document the exposure. **This is independent of the refactor and should not block it.**

---

## Current architecture

```
  React SPA (nginx) ──/api/──► gunicorn ─► config.urls ─► tweets.urls
                                                │
                                   tweets/views.py ······ ~100 lines of feed query policy
                                   tweets/analytics.py ··· views + params + raw SQL (1056 ln)
                                   tweets/serializers.py · runs its own ORM queries
                                                │ 35 deferred imports ⇄ cycle
                                        fetching/*  (Django app: tasks, ingest, runner)
                                                │ subprocess: python -m fetcher.<pipeline>
                                        fetcher/*   (pure engine — zero Django imports)
```

The `fetcher ↔ fetching` split is **healthy** — one import edge
(`fetching/accounts.py:13`), everything else crosses as argv + `TDF_*` env vars, and data
returns over the scratch disk. The engine imports Django nowhere. Leave the boundary alone;
only the *name* is the problem.

The `tweets ↔ fetching` entanglement is the real layering violation.

## Target architecture

Boring, conventional Django + DRF. Same apps, same names for everything on the wire:

```
backend/
  config/            settings, urls, celery, wsgi, middleware, pagination   (unchanged)
  tweets/            ← Django app: the data + the HTTP surface
    models.py                      (unchanged)
    urls.py                        (unchanged — same routes, same names)
    views.py         transport only: parse, authorize, delegate, serialize
    auth_views.py                  (unchanged — §5 off-limits)
    analytics.py     transport only: the 8 analytics APIViews
    feed.py          NEW  feed_queryset + its helpers, moved out of views.py
    stats.py         NEW  the raw analytics SQL, moved out of analytics.py
    windows.py       NEW  Window / parse_instant / window_from / normalize_handles /
                          parse_since — the one home for "what time range, which accounts"
    serializers.py, params.py, permissions.py, topics.py, textclean.py, admin.py
  fetching/          ← Django app: orchestration. Celery task names UNCHANGED.
  engine/            ← renamed from fetcher/. Pure library + CLIs. No Django.
```

```
frontend/src/
  main.jsx  App.jsx  index.css
  pages/          (unchanged; + AuthLayout moves out)
  layouts/        AuthLayout
  components/     TweetCard BudgetRail InfiniteSentinel LegacyRedirect Logo ErrorBoundary
                  AccountPicker Avatar
  components/ui/  button controls dialog field panel status tabs
  hooks/          usePoll useMediaQuery useAccounts
  lib/            api cn format series chartTokens windowParams
  context/        auth
```

Rule the layout follows: transport (`views`/`pages`) never holds query or business logic;
nothing in `tweets/` imports `fetching/` except through a top-level import that does not
cycle; `engine/` imports nothing from the Django project.

---

## Batches (ordered, lowest risk first)

Every batch follows the Landing Protocol: clean tree → one change category → ruff + pytest +
`npm run lint`/`test`/`build` + `manage.py check` + boot and hit a real endpoint → grep the
whole tree for every removed identifier → `git diff` line by line → push → watch the deploy
and the logs before the next one.

| # | Batch | Risk | Diff | Verification beyond the standard gates |
|---|---|---|---|---|
| 0 | Commit the pending `DJANGO_SECURE_SSL: "1"` in `docker-compose.prod.yml` (approved; closes e2e finding F-002) | med | ~3 | After deploy, confirm login still works over HTTPS and `Set-Cookie` carries `Secure`. HSTS is a **one-year, hard-to-reverse browser commitment** — see risks. |
| 1 | `.gitignore` += `work/`, `.qa/`, `tmp/`, `tdf-runtime-health.html` | none | ~6 | `git status` clean |
| 2 | Frontend dead exports: drop `DialogClose`, `buttonVariants`, `TextField`, `BAR_RADIUS_X`; un-export `STATUS`/`AXIS`/`SURFACE`; drop `usePoll`'s redundant default export; delete the duplicate import line at `QueryDialog.jsx:5-6` | low | ~30 | grep each symbol → 0 hits |
| 3 | Remove `@testing-library/dom` from devDependencies (transitive peer, zero direct imports). One dep, one commit. | low | lockfile | `npm ci && npm test` from a clean `node_modules` |
| 4 | **`tweets/windows.py`** — move `Window`, `parse_instant`, `window_from`, `normalize_handles`, `accounts_from`, and `parse_since` into one module. Kills `analytics.py → fetching.management.commands.fetch_report` and `exports.py → tweets.analytics`. | low | ~180 | `test_analytics_api`, `test_fetch_report`, `test_export_jobs` |
| 5 | **`tweets/feed.py`** — move `feed_queryset`, `with_feed_ts`, `feed_instant`, `_calendar_start`, `_normalize_handle` out of `views.py`. `fetching/exports.py` then imports it at module level. | med | ~200 | `test_api`, `test_ranked_feed`, `test_feed_fixes`, `test_export_jobs`; diff one real `/api/feed/` response before/after |
| 6a | **`tweets/stats.py`** — move the ingestion/pipeline raw SQL (`_request_spend`, `_rate_limits`, `_endpoint_health`, `_engagement_sql`) out of `analytics.py` | med | ~300 | `test_analytics_api`, `test_raw_sql_placeholders` |
| 6b | Move the velocity / topics / narratives SQL to `stats.py`; `analytics.py` is left holding only the 8 APIViews | med | ~350 | same, **plus a run against local Postgres** — SQLite skips these paths entirely |
| 7 | Move `REPORTED_ENDPOINTS` to a neutral module, then convert the 35 deferred imports in `tweets/{views,serializers,admin}.py` to top-level | med | ~60 | `manage.py check` + real boot of web **and** every worker — an import cycle only shows at boot |
| 8a–c | Frontend moves: hooks → `hooks/`, utils → `lib/`, components → `components/`. One category per batch, pure moves + import rewrites. Standardise on the `@/` alias while moving. | low | ~150 ea | `npm test` + `npm run build`; eyeball each route in the dev server |
| 9 | `fetcher/` → `engine/`. Touches ~100 imports, the three subprocess module strings, `runner.py` PYTHONPATH, README, AGENTS.md — **and `ci.yml`'s `compileall` list, which §5 puts off-limits.** Needs its own approval. | **high** | ~250 | `compileall`, full pytest, and a real `fetch_account_live` run against the live engine before pushing |
| 10 | `ARCHITECTURE.md` (layer diagram + directory map + the rules); README trimmed to what is true; `.env.example` gains `VITE_API_PROXY` | none | ~150 | read it against reality |

`REFACTOR_PLAN.md` gets committed and pushed on its own first, per §1.4.

Batches 6a/6b exceed the 400-line ceiling as *moves*; the ceiling exists so a revert is
cheap, and a pure file split reverts cleanly. I will keep them move-only — not one logic line
edited — so `git diff -M` reads as a rename.

---

## Deletion candidates

| Path / symbol | Evidence | Confidence | If wrong |
|---|---|---|---|
| `ui/dialog.jsx DialogClose`, `ui/button.jsx buttonVariants`, `charts.js BAR_RADIUS_X` | zero hits repo-wide incl. tests | high | build fails immediately |
| `pages/AuthLayout.jsx TextField` | zero hits; only sibling `PasswordField` is imported | high | Login/Signup render break, caught by their tests |
| `usePoll` default export | all 4 callers use the named import | high | import error at build |
| `@testing-library/dom` | zero direct imports; peer of `@testing-library/react` | medium | `npm ci` test run fails |
| `filters.jsx:12` pass-through re-export of `Segmented`/`ToggleChips` | pure forward; cause of two import styles for one component | high | 2 call sites, both updated in the same batch |
| `charts.js` re-declared color literals | duplicates `index.css` tokens | medium | charts lose color — **report only**, no runtime token reader exists |

**Suspected dead — needs runtime confirmation, NOT deleted this pass:**

- `fetcher/auth.py` (471 lines). Zero programmatic callers — no management command, no task,
  no script. It is an operator CLI (`tdf-auth --interactive`) and `fetcher/timeline.py:335`
  names it in an error message — it is how a stale `x-client-transaction-id` gets refreshed,
  which is the documented cause of endpoint 404s. **Keep.** Instrument: log a line on entry,
  ship, check in 30 days.
- `FETCH_HISTORICAL_QUOTA_FLOOR`. `runner.py:613-619` overwrites it with a computed value for
  the *only* subsystem that reads it, so the documented setting is effectively dead. Report,
  don't remove — it's a documented env var.
- `getAccessToken` (`api.js:23`). Referenced only by `api.test.js`. Plausibly part of the
  module's intended surface. Keep.

---

## Reported, not fixed (§0.1 — these are behavior changes)

- **Three polling implementations, two different behaviors.** `usePoll` pauses on a hidden
  tab; `useLiveRefresh` (`filters.jsx:196`) does not, so Dashboard and Analyze keep hitting
  the API in a background tab; `Feed.jsx:162` is a raw 30s `setInterval` that is neither.
  Merging them would change request volume. Needs a decision, then its own PR.
- **Two tweet-timestamp parsers that disagree.** `fetcher/processing.py:464` returns
  Tehran-local and rejects ISO-8601; `fetching/ingest.py:70` returns UTC and accepts it.
  Same input format, two answers for "when was this posted".
- **`FETCH_EMPTY_PAGE_STREAK` clamps on one side only** — the engine floors it at 2,
  `settings.py` does not, so a value of 1 makes the two layers silently disagree.
- **Run-duration rendered two different ways** — `Ops.jsx:24` hand-rolls it, `Workflow.jsx:28`
  uses `format.duration`, for the same `FetchRun`.
- **Serializer N+1s** — `serializers.py:335` falls back to `obj.hits.count()` per row when the
  viewset annotation is absent; `get_last_run` has two query paths.
- **Workers don't wait for the migration.** `depends_on` covers only postgres/redis, so during
  a deploy a worker can execute against the pre-migration schema.
- **No rollback artifact.** `docker image prune -f` at the end of every deploy destroys the
  image a rollback would need.

Data layer: **no table, column or index is touched.** `Tweet` and `SearchTweet` are two tables
for one entity, but that is deliberate and documented ("two collectors, two tables"), and §5
forbids contraction. Indexes look well-matched to the raw SQL. Migrations are append-only and
untouched.

---

## Open questions / risks

1. **Batch 0 turns on HSTS for one year with preload+subdomains.** A browser that sees it
   will refuse plain HTTP to the domain for 12 months, and a revert does not un-teach it.
   CI's `check --deploy` already runs with `DJANGO_SECURE_SSL=1`, and the prod overlay
   already sets it via the `x-prod-app` anchor — so I want to confirm the diff is genuinely
   adding it rather than duplicating it, before pushing.
2. **Batch 9 requires editing `ci.yml`** (one word in the `compileall` argument list). §5
   puts the pipeline off-limits without approval. If that's a no, the rename is off and
   `fetcher/` keeps its name — I'll note the confusion in `ARCHITECTURE.md` instead.
3. **No staging and no rollback image.** Every batch's only safety net is `git revert` + a
   full ~5-minute redeploy. I'll verify the revert path works on batch 1, the harmless one,
   rather than discovering it during a real incident.
4. `/opt/apps/deploy_twitter.sh` on the VPS decides which commit ships and is not in this
   repo. I cannot audit it.
5. The `simplify` branch is already wired into CI triggers but not deploy. I'll land on
   `main` per the brief unless you'd rather I stage batches there first.

## Verification

Per batch, recorded in the commit body: `ruff check .`; `python -m pytest -q` (443 tests) with
`TEST_POSTGRES_*` pointed at a local Postgres 16 — SQLite skips the raw-SQL analytics paths
entirely and accepts input Postgres rejects; `python manage.py check --deploy
--fail-level WARNING`; `npm run lint && npm test && npm run build`; `docker compose up -d
--wait` then `curl /api/health/`, one `/api/feed/` and one `/api/analytics/topics/` response
diffed against a pre-change capture; and `grep -rn` over the whole tree — including
`ci.yml`, both compose files, Dockerfiles, `nginx.conf`, `AGENTS.md`, `README.md` and string
literals — for every identifier moved or removed.

After push: watch the Actions run to green, `curl` the live health endpoint, then read
`docker compose logs --since` on `web` and all four workers for import errors and 5xx before
starting the next batch. Progress report every 3–4 landed batches.
