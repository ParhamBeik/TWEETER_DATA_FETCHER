# TWEETER DATA FETCHER — Agent Guide

Latest update: July 18, 2026.

This file is for coding agents. For human install/usage, see `README.md`.

## Active Architecture

Canonical package: setuptools `src` layout under `twitter_fetcher/src/tweeter_data_fetcher/`.

`twitter_fetcher/` holds all current-version code, tests, diagnostics, config, and runtime data. Repo root holds only that parent, `LEGACY/` (read-only archive), `.venv/`, `graphify-out/` (local, gitignored), and metadata (`README.md`, `AGENTS.md`, `pyproject.toml`, `.gitignore`).

`paths.py` is the single path router. `PROJECT_ROOT` is `twitter_fetcher/` (not the git root). All `config/` and `data/` paths resolve from there.

| Responsibility | Canonical location |
|---|---|
| Historical pipeline | `twitter_fetcher/src/tweeter_data_fetcher/pipelines/historical/service.py` |
| Live pipeline | `pipelines/live/service.py`, `state.py`, `viral.py` |
| Search pipeline | `pipelines/search/service.py`, `query.py` |
| Runnable entrypoints | Pipeline `service` modules, `x_api/auth.py`, `observability/coverage_inventory.py` |
| HTTP transport | `x_api/client.py` (`APIManager`) |
| curl_cffi transport | `x_api/curl_cffi_client.py` |
| Request persistence | `x_api/request_state.py` |
| GraphQL contracts | `x_api/contracts.py`, `contract_verification.py` |
| Pagination | `x_api/timeline.py` (`FetcherEngine`) |
| Auth/browser | `x_api/auth.py`, `browser.py` |
| Tweet processing | `processing/` (`core.py`, `parsing.py`, `sets.py`, `windows.py`) |
| Storage | `storage/` (`facade.py` = `StorageManager`) |
| Observability | `observability/` |
| Path constants | `paths.py` |
| Configuration | `configuration.py`, `account_config.py`, `twitter_fetcher/config/` |
| Diagnostics | `twitter_fetcher/diagnostics/` |
| Tests | `twitter_fetcher/tests/{unit,integration,contract}/` |

### Rename map (do not resurrect old paths)

| Old | Current |
|---|---|
| `src/`, `tests/`, `config/`, `tools/` at repo root | under `twitter_fetcher/` |
| `tweeter_data_fetcher/twitter/` | `tweeter_data_fetcher/x_api/` |
| `tweeter_data_fetcher/tweets/` | `tweeter_data_fetcher/processing/` |
| `tools/diagnostics/` | `twitter_fetcher/diagnostics/` |
| `tests/reports/` | `twitter_fetcher/diagnostics/reports/` |

## Non-Negotiables

- Never commit `twitter_fetcher/config/config.json`, `twitter_fetcher/data/`, diagnostic run output, or `graphify-out/`.
- Prefer root-cause fixes in canonical code under `twitter_fetcher/src/`.
- Preserve endpoint result dictionaries, reports, state schemas, watermarks, seven processed folders, validation-run isolation, and all `data/` path shapes.
- Keep Twitter/X request changes evidence-backed by diagnostic captures.
- Do not add database layers, abstract repositories, ports, factories, or new dependencies without a current second implementation.
- After meaningful code changes run `.venv/bin/python -m pytest -q`.
- After architecture or path moves run `graphify update .` (use `--force` / `GRAPHIFY_FORCE=1` if outputs stay untouched after a large rename). Before exploring with Read/Grep/Glob, orient with `graphify query` / `path` / `explain` when `graphify-out/graph.json` exists.

## Configuration

Resolution order:

1. Explicit path
2. `TDF_CONFIG`
3. `twitter_fetcher/config/` (via `PROJECT_ROOT`)

Tracked files:

- `twitter_fetcher/config/config.example.json`
- `twitter_fetcher/config/accounts.json`
- `twitter_fetcher/config/searches.json`

Move local secrets by rename; never copy them into tracked files.

## Commands

```bash
source .venv/bin/activate
pip install -e .

tdf-historical --only elonmusk
tdf-live --account elonmusk --once
tdf-search --once
tdf-coverage --format table
tdf-auth --interactive
```

Entry points are defined in `pyproject.toml` (`package-dir` = `twitter_fetcher/src`). Pytest uses `testpaths = ["twitter_fetcher/tests"]`.

## Runtime Contracts

Historical and live share `twitter_fetcher/data/historical_live/` and use global two-pass order:

1. Resolve user IDs.
2. Fetch `UserTweets` for every active/due account.
3. Fetch `UserTweetsAndReplies` for every active/due account.
4. Build processed sets.

Rolling cutoff:

```text
effective_cutoff = min(now - configured_window, floor(fetch_watermark))
```

- Historical floors watermark to day start.
- Live floors watermark to hour start.
- Watermarks advance only after successful endpoint completion.
- Partial/failed runs do not advance watermarks.
- Overlap is expected and deduplicated.

Processed folders (unchanged):

1. `1_user_tweets`
2. `2_user_tweets_and_replies`
3. `3_intersection`
4. `4_union`
5. `5_a_minus_b`
6. `6_b_minus_a`
7. `7_symmetric_difference`

`StorageManager.merge_processed_items()` deduplicates by `author_id:tweet_id`, falling back to tweet ID. Legacy `5_replies_only` maps to `6_b_minus_a`.

## HTTP And GraphQL

- `APIManager` owns transport/auth/session behavior.
- `RequestStateStore` owns JSON persistence for tx/query health, rate limits, and endpoint health.
- Tx/query candidates are suspect on failures one and two, stale on failure three, and healthy/reset on HTTP 200.
- Timeline endpoints use GET with compact JSON `variables`, `features`, and optional `fieldToggles`.
- Search omits `fieldToggles`; profile timelines use `{"withArticlePlainText":false}`.
- Extract only bottom cursors; never reuse them across endpoint/account/query/product/session.
- Validate status, JSON, GraphQL errors, endpoint data path, instruction types, and fresh cursor independently.
- HTTP 429 sleeps to reset plus safety buffer, bounded by the configured maximum.

## Storage

Runtime root is `twitter_fetcher/data/` (`DATA_DIR` in `paths.py`):

```text
twitter_fetcher/data/
  historical_live/
    raw/UserTweets/{account}/{batch}/page_N.json
    raw/UserTweetsAndReplies/{account}/{batch}/page_N.json
    processed/{seven set folders}/
    reports/
    state/
    logs/
    viral/
  search/
    raw/{search_slug}/{product}/{batch}/page_N.json
    processed/{search_slug}/{product}/
    debug/
    reports/
    logs/
    state/search_state.json
  validation/{run_id}/
```

Search must not create historical/live set folders.

## Observability Contract

- `PipelineConsole` owns tagged terminal output and forwards every message to the package logger.
- `configure_logging()` writes rotating subsystem logs under `data/<subsystem>/logs/` and stamps records with `run_id`.
- `EventRecorder` writes structured `events.jsonl`, HTTP detail files under `logs/errors/`, and `http_summary.json`.
- Historical emits run and phase events; live/search emit cycle events; timeline pagination emits page events; HTTP failures emit detail references.
- Event/log write failures must be logged, not silently discarded.
- Do not log cookies, bearer tokens, CSRF tokens, or full authorization headers in ordinary messages. HTTP detail files are local runtime artifacts and must remain ignored.

## Diagnostics

```bash
python twitter_fetcher/diagnostics/verify_contract.py
python twitter_fetcher/diagnostics/probe_txid.py
python twitter_fetcher/diagnostics/probe_dynamic_txid.py
python twitter_fetcher/diagnostics/probe_sequence.py
python twitter_fetcher/diagnostics/probe_pacing.py
python twitter_fetcher/diagnostics/pagination_test.py
python twitter_fetcher/diagnostics/traffic_sniffer.py
```

Reports: `twitter_fetcher/diagnostics/reports/`. Probe/sniffer run dirs (`probe_runs/`, `sniffer_runs/`, `graphql_logs/`) are gitignored.

`FetcherEngine` invokes contract verification directly as a library function. No subprocess launch is allowed for startup verification.

## Tests

```bash
.venv/bin/python -m pytest -q
python -m compileall -q twitter_fetcher/src twitter_fetcher/tests twitter_fetcher/diagnostics
```

Current suite: **108 passed** on July 18, 2026.
