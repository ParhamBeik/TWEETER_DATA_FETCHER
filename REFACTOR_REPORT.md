# Refactor report

13 commits landed on `main`, each deployed to production and verified before the
next one started. No behaviour changed, no route moved, no task was renamed, no
schema was touched.

The headline is not lines removed — it is that the two apps no longer import each
other in a circle, no view module contains SQL, and the two backend packages no
longer have names that differ by three letters.

## By the numbers

| | before | after |
| --- | --- | --- |
| tracked files | 223 | 228 |
| `tweets/analytics.py` | 1056 lines | 492 |
| `tweets/views.py` | 716 | 533 |
| function-local imports `tweets/` → `fetching/` | 35 | 0 |
| `connection.cursor()` in a view module | 5 | 0 |
| files at `frontend/src/` root | 24 | 5 |
| unreferenced frontend exports | 6 | 0 |
| direct npm dependencies | 29 | 28 |

Net `+1680 / −1174` across 116 files. The file count went *up*, by five: three
new backend modules that logic moved into, plus `ARCHITECTURE.md` and this
report. That is the point — nothing was deleted that had a caller, and the win
came from putting things where they belong rather than from deleting them.

## Architecture, before and after

```
BEFORE                                   AFTER

backend/                                 backend/
  config/                                  config/
  tweets/                                  tweets/
    views.py     ─┐ feed query policy        views.py      transport only
    analytics.py ─┤ views+params+raw SQL     analytics.py  transport only (8 views)
    serializers.py│ runs its own queries     feed.py       ← feed query policy
        ↕ 35 deferred imports ↕              stats.py      ← every analytics query
  fetching/      ─┘                          windows.py    ← window/account parsing
  fetcher/         ← name collision        fetching/       one-way: → tweets, → engine
                                           engine/         ← renamed; imports no Django
```

`fetcher/` → `engine/`. `fetching/` deliberately keeps its name: its module path
is baked into 15 Celery task names shared with `task_routes`, the beat schedule
and any job already queued on Redis.

Frontend: `src/` root went from 24 files — pages, shared components, hooks,
utilities and their tests, all loose — down to 5, into
`components/ hooks/ lib/ context/ layouts/` beside the existing `pages/` and
`ui/`.

## The commits

| sha | what |
| --- | --- |
| `0dd09c6` | Phase 0 plan, committed on its own |
| `85380fb` | prod: `DJANGO_SECURE_SSL=1` on the compose anchor |
| `ab01427` | prod: set it on `web` too, where a YAML merge key was shadowing it |
| `8990f49` | gitignore operator scratch (`work/`, `.qa/`, `tmp/`) |
| `459f14d` | drop 6 unreferenced frontend exports |
| `f528556` | drop the direct `@testing-library/dom` devDependency |
| `9f85321` | `tweets/windows.py` — one home for window/account parsing |
| `1fe940d` | `tweets/feed.py` — feed query out of the view layer |
| `07519bf` | `tweets/stats.py` — analytics queries out of the analytics views |
| `5c359ae` | last two inline SQL blocks into the query layer |
| `dc5a955` | 35 deferred imports → ordinary top-level ones |
| `f1767f8` | conventional Vite + React layout for `frontend/src` |
| `8878266` | `fetcher/` → `engine/` |
| `4d3e24d` | `ARCHITECTURE.md`; corrected four false claims in the README |

## How "no behaviour change" was established

Tests alone would not have been enough, so each risky batch carried its own
evidence:

- **Feed query move**: a probe rendered the SQL `feed_queryset` generates for 15
  parameter sets — every sort, every window kind, multi-account, tier, `run_id`,
  `q`, `has_media`, `include_untracked` and a combined case — against PostgreSQL
  18 on both sides. Byte-identical once the wall-clock instant rolling windows
  bake in is normalized.
- **Analytics extraction**: 24 seeded tweets across two accounts with overlapping
  text and two metric snapshots each, then all 11 analytics endpoint/parameter
  combinations through DRF's test client — including non-empty velocity rankings
  and non-empty narrative pairs. 3808 lines of JSON, identical before and after.
- **Deferred-import conversion**: a cycle shows at boot, not in a test. Each of
  ten modules was imported *first* in a fresh interpreter; all 15 Celery task
  names still register.
- **Package rename**: all five entrypoints launched by module string
  (`python -m engine.{historical,live,search,probe,auth}`), since these are
  reached only as strings and a miss would have failed a Celery task at runtime
  rather than a test. Confirmed in production afterwards: between the two
  deploys four minutes apart, search pages went 306 → 307 and upserts 6120 →
  6140, i.e. the renamed engine completed a real fetch.
- **Frontend reorg**: the built bundle was served through `vite preview` and all
  16 emitted chunks fetched 200, so no lazy route import pointed at a moved file.

The suite ran against **PostgreSQL**, not SQLite, throughout: the analytics
queries check `connection.vendor` and return early, so a green SQLite run says
nothing about them. 553 tests, 1 skipped, on every batch.

## Two things I got wrong

Both were caught before they could do damage, and both are worth recording
because they are the failure modes this kind of work actually has.

1. **`85380fb` was inert.** I added `DJANGO_SECURE_SSL` to the `x-prod-app` YAML
   anchor. A merge key does not deep-merge: `web` declares its own
   `environment:` block, which replaced the anchor's outright, so the one service
   that serves HTTP and sets cookies never saw it. Caught by checking the
   deployed site rather than the deploy's exit code — the `csrftoken` cookie
   still had no `Secure` flag. Fixed in `ab01427`; now verified by rendering the
   merged compose config, not by reading the file.

2. **The rename rewrote a logger name.** A regex over quoted dotted paths turned
   `getLogger(f"fetcher.console.{subsystem}")` into `"engine.console..."`, which
   stopped the console logger being a child of `ROOT_LOGGER_NAME` and silently
   broke propagation to the file handler. `test_logging_setup` caught it. It is
   now built from the existing `CONSOLE_LOGGER_PREFIX` constant so the two cannot
   drift, and the runtime name is verified to still be `fetcher.console.search`.

## Bugs found, not fixed

Behaviour changes, so out of scope for a refactor pass. Ranked by how much they
matter.

1. **`FETCH_HISTORICAL_QUOTA_FLOOR` is a dead setting.**
   `backend/fetching/runner.py:613-619` computes the floor from the live due-set
   and overwrites the configured value for the `historical` subsystem — the only
   subsystem that reads it. The env var only affects CLI runs. Documented in the
   README for now; decide whether to remove it or rename it to say it is a
   CLI-only fallback.

2. **Two tweet-timestamp parsers that disagree.**
   `backend/engine/processing.py:464` parses with `strptime` and returns
   Tehran-local, rejecting ISO-8601. `backend/fetching/ingest.py:70` uses
   `parsedate_to_datetime`, returns UTC, and accepts ISO-8601. Same input format,
   two answers to "when was this posted", on either side of the process boundary.

3. **`FETCH_EMPTY_PAGE_STREAK` clamps on one side only.**
   `backend/engine/timeline.py:70` floors it at 2; `backend/config/settings.py:331`
   does not. Setting it to 1 makes the two layers silently disagree. The settings
   comment explains why one page too few is the expensive direction.

4. **Three polling implementations, two behaviours.**
   `hooks/usePoll.js` pauses on a hidden tab. `components/filters.jsx:196`
   `useLiveRefresh` does not — so Dashboard and Analyze keep hitting the API in a
   background tab. `pages/Feed.jsx:162` is a raw 30s `setInterval` that is
   neither. Merging them changes request volume, so it needs a decision first.

5. **Run duration rendered two ways.** `pages/Ops.jsx:24` hand-rolls it and
   always prints `"Ns"`; `pages/Search/Workflow.jsx:28` routes the same
   `FetchRun` fields through `lib/format.duration`.

6. **Serializer N+1s.** `backend/tweets/serializers.py:335` falls back to
   `obj.hits.count()` per row when the viewset annotation is absent;
   `get_last_run` has two query paths.

7. **Workers do not wait for the migration.** `depends_on` covers only
   `postgres` and `redis`, so during a deploy a worker can execute against the
   pre-migration schema.

## Suspected dead — needs runtime confirmation

Not deleted. Each would need instrumentation shipped first.

- **`backend/engine/auth.py`** (471 lines). No programmatic caller — no task, no
  management command, no script. It is the operator CLI `tdf-auth --interactive`,
  and `engine/timeline.py:335` names it in an error message; it is how a stale
  `x-client-transaction-id` gets refreshed, which is the documented cause of
  endpoint 404s. **Keep.** To confirm: log a line on entry, ship, read the logs
  in 30 days.
- **`getAccessToken`** (`frontend/src/lib/api.js:23`). Referenced only by its own
  test. Plausibly intended module surface; too cheap to be worth the risk.

## Left alone deliberately

- **The `engine/` ↔ `fetching/` split.** It looked like duplication and is not:
  one import edge (`fetching/accounts.py:13`), everything else crossing as argv,
  `TDF_*` env vars and the scratch disk, and zero Django imports in `engine/`.
  That is the healthiest structural property in the backend. Only the name was
  the problem.
- **The four cursor-pagination blocks** in Feed, Accounts, Ops and Search. They
  share a shape, which is why they look mergeable. They do not share behaviour:
  the race guard is keyed on a counter, a handle, a generation and an id
  respectively; Feed guards only the append path (with a comment recording that
  guarding both dropped submitted filters); Ops re-checks the generation on the
  error path (comment: a page-two failure painted over an already-replaced list);
  Search rewrites DRF's "No Search matches the given query." into human language.
  Every difference is a recorded incident fix. A hook with enough options to
  preserve all four would be harder to read than the four copies.
- **`ROOT_LOGGER_NAME = "fetcher"`** inside `engine/`. A runtime logger namespace
  in every engine log line on the VPS; renaming it changes output operators grep.
- **`Tweet` / `SearchTweet` as two tables.** Deliberate and documented.
- **Auth, throttling, CORS, CSRF, security headers, migrations, the data model.**
  Untouched by policy. No table, column or index was added, altered or dropped.
- **`backend/config/settings.py`.** Already one validated config module that
  fails loudly on a bad value, with every env var read exactly once. There was
  nothing to unify.
- **`self.fetcher` / `FetcherEngine` / `run_fetcher`.** They name a class and its
  instances, not the package.

## Recommended follow-ups, ranked

1. **Rotate the shared X session credential, then decide on history surgery.**
   `git log --all --diff-filter=A` shows `config.json` and `search_config.json`
   were committed under the legacy `TWEETER DATA FETCHING 1.0`–`4.0` trees. The
   repo's own `.gitignore` documents that `config.json` holds `auth_token`, a
   full login to the shared X account. They are gone from `HEAD` but reachable in
   history. I did not read the values and did not push a commit deleting them —
   deletion does not remove history. Rotation first; then `git filter-repo`/BFG
   plus a coordinated force-push, or an accepted-and-documented exposure. The
   history also contains a fully committed `frontend/node_modules`.
2. **Give the deploy a rollback artifact.** There is none today: nothing is
   tagged or pushed to a registry, and `scripts/deploy_vps.sh:82` runs
   `docker image prune -f` at the end of every deploy, destroying the image a
   rollback would need. The only recovery is `git revert` plus a full rebuild.
   Tagging each build with the commit sha before pruning would cost one line.
3. **Make workers wait for the migration**, or gate them on `web` being healthy.
4. **Decide on the polling hooks** (bug 4). Background tabs currently poll.
5. **Resolve the two timestamp parsers** (bug 2) with a shared helper and a test
   asserting both spellings of the input produce the same instant.
6. **Add a test pinning the `TDF_*` default pairs.** `settings.py:320,324,331,351`
   duplicate literals that `engine/historical.py:45,49,52` and
   `engine/timeline.py:70` also carry. They agree today; nothing keeps them so.
7. **No staging environment.** Every change here was verified against production
   because there is nowhere else. That constrained batch size all the way
   through and will constrain the next piece of work too.
