# Agent guide

For install and usage see `README.md`. This file is the working contract.

## Where things live

| Concern | File |
| --- | --- |
| Celery tasks, scheduling, locks | `backend/fetching/tasks.py` |
| Subprocess runner + Postgres state round-trip | `backend/fetching/runner.py` |
| Tweet upsert / dedup / metric snapshots | `backend/fetching/ingest.py` |
| Saved-search schedule state and teardown | `backend/fetching/searches.py` |
| Models (sole durable store) | `backend/tweets/models.py` |
| API | `backend/tweets/views.py`, `analytics.py`, `auth_views.py`, `urls.py` |
| Topic ranking (pure, no DB) | `backend/tweets/topics.py` |
| Auth (JWT) + the staff gate | `backend/tweets/auth_views.py`, `permissions.py` |
| HTTP transport, tx/query-id health | `backend/fetcher/client.py` |
| Pagination engine | `backend/fetcher/timeline.py` |
| Pipelines | `backend/fetcher/{historical,live,search}.py` |
| Tweet parsing + rolling window + GraphQL contracts | `backend/fetcher/processing.py` |
| Scratch-disk layer | `backend/fetcher/storage.py` |
| Console, file logs, NDJSON events | `backend/fetcher/observability.py` |
| Paths, config resolution, account tiers | `backend/fetcher/config.py` |
| Design tokens (colour, type, spacing) | `frontend/src/index.css` |
| UI primitives (button, panel, status, dialog, tabs) | `frontend/src/ui/` |
| Shared X budget rail, on every page | `frontend/src/BudgetRail.jsx` |
| Shared console filter bar, account picker, avatar | `frontend/src/filters.jsx` |
| Chart tokens, status/subsystem colours, series pivot | `frontend/src/charts.js` |
| Number, time and permalink formatting | `frontend/src/format.js` |

## Non-negotiables

- Postgres is the only durable store. `backend/fetcher/` writes to an ephemeral
  scratch root (`TDF_PROJECT_ROOT`) that is deleted after every run; anything
  that must survive goes through `KeyValueState`/`EndpointState` in `runner.py`.
- Never commit `.env`. Never log cookies, bearer tokens, CSRF tokens, or full
  authorization headers — `fetching/redaction.py` is the single choke point and
  every subprocess line passes through it.
- Do not add database layers, abstract repositories, ports, or factories
  without a second real implementation. Do not add dependencies that stdlib or
  an existing dependency already covers.
- `docker-compose.yml` pins `name: twitter-saas`. That is what keeps the volume
  called `twitter-saas_pgdata`. Changing or removing it orphans the production
  database.
- Celery task `name=` strings are wire identifiers. Renaming one strands any
  message already queued under the old name.
- Run `cd backend && python -m pytest -q` after code changes.

## Runtime contracts

Historical and live share `data/historical_live/` and run a two-pass order:
resolve user IDs, fetch `UserTweets` for every due account, write `4_union`.

```
effective_cutoff = min(now - configured_window, floor(fetch_watermark))
```

- Historical floors the watermark to the start of the Tehran day, live to the hour.
- Watermarks advance only after a successful endpoint completion; a partial or
  failed run must not advance one.
- Overlap is expected and deduplicated by `author_id:tweet_id`.
- `Tweet.source_subsystem` records which pipeline *first* captured a row and is
  written on insert only — it is deliberately absent from
  `ingest._TWEET_UPDATE_FIELDS`. Live re-sees backfilled tweets constantly, so
  making it mutable would credit the whole archive to `live` within two cycles
  and the console's collection-flow chart would be a lie. `source_endpoint`
  cannot substitute: live and the archive walk both hit `UserTweets`.
- `4_union` is the only processed output. Do not reintroduce the other six set
  folders, `UserTweetsAndReplies`, or set algebra.
- Accounts quarantine after three consecutive user-ID resolution failures.
  Clearing quarantine must clear both the Postgres row and the live state blob.
- All three fetchers share one X rate budget. The archive walk must always
  leave `FETCH_HISTORICAL_QUOTA_FLOOR` requests for live polling; anything that
  can paginate needs a stop condition that does not depend on the API
  withholding a cursor, since a timeline past its last tweet keeps offering
  one.
- The archive walk keeps its own `backfill_cursor` in the endpoint state blob.
  Never resume it from `last_cursor` -- the shallow live poll writes that, and
  the two walks sit at different depths in the timeline.
- Signup is open and new users are non-staff. Any endpoint that spends X quota
  or touches the shared session needs `IsStaff`/`IsStaffOrReadOnly`; the
  project-wide default is only `IsAuthenticated`.
- Search stays isolated under `data/search/` and never creates historical/live
  set folders, and in Postgres it stays isolated in `SearchTweet`/`SearchHit`.
  `Tweet` is the tracked-account (`UserTweets`) archive and nothing else: the
  feed reads it, the engagement analytics read it, and it has no TTL. Search hits
  have their own 30-day clock. Never write a search result into `Tweet`.
- A search's scratch state is keyed two different ways and both spellings must be
  exact: `EndpointState.account` is `<slug>::<product>` (double colon, from
  `fetcher.search._state_key`) and `RawPage.account` is `<slug>:<product>` (single
  colon, from the raw path join). `fetching/searches.py` holds both; teardown that
  guesses one deletes nothing and leaves a live cursor behind.
- Deleting a `Search` goes through `fetching.searches.teardown_search`, never a
  bare `.delete()`. Anything less strands raw pages, cursors and run history.
- Topic ranking lives in `tweets/topics.py` and is pure. The SQL in `analytics.py`
  only groups -- it counts documents and distinct authors, never occurrences, and
  it excludes retweets. Ranking logic must not migrate back into the query: the
  raw-SQL analytics paths are skipped entirely on the SQLite test database.

## HTTP and GraphQL

- `APIManager` owns transport, auth, and session behaviour; `RequestStateStore`
  owns tx/query health, rate limits, and endpoint health persistence.
- Tx/query candidates are suspect on failures one and two, stale on three,
  healthy again on any HTTP 200.
- Timeline endpoints use GET with compact JSON `variables`, `features`, and
  optional `fieldToggles`. Search omits `fieldToggles`; profile timelines send
  `{"withArticlePlainText": false}`.
- `SearchTimeline` page 1 goes over HTTP; deeper pages use the Playwright
  hybrid. Mid-pagination cursor 404s must fail fast via `_cursor_gate` rather
  than burning a multi-minute retry loop.
- Extract bottom cursors only, and never reuse one across
  endpoint/account/query/product/session.
- Status handling: `400` stop (contract broke), `401`/`403` pause for credential
  refresh, `404` classify by endpoint/cursor without mutating secrets,
  `5xx`/network bounded retry, `429` sleep to reset plus buffer.

## Observability

- `PipelineConsole` owns terminal output and forwards every message to the
  package logger, so nothing is printed that is not also on disk.
- `EventRecorder` writes `events.jsonl`, HTTP detail files under `logs/errors/`,
  and `http_summary.json` including the failure ledger.
- `FetchRun.status` is one of `running`, `completed`, `partial`, `failed`,
  `auth_required`. A run that exits 0 but writes no report is `partial`, not
  `completed` — "did nothing" must not look healthy.
- Event and log write failures are logged, never silently swallowed.
