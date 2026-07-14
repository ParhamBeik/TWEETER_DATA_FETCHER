# TWEETER DATA FETCHER — Agent Guide

Latest update: July 14, 2026.

## Active Architecture

The canonical codebase is the standard setuptools `src/` package under `src/tweeter_data_fetcher/`. `LEGACY/` is read-only archive material.

| Responsibility | Canonical files |
|---|---|
| Historical pipeline | `src/tweeter_data_fetcher/pipelines/historical/service.py` |
| Live pipeline | `src/tweeter_data_fetcher/pipelines/live/service.py`, `state.py`, `viral.py` |
| Search pipeline | `src/tweeter_data_fetcher/pipelines/search/service.py`, `query.py` |
| Runnable entrypoints | Pipeline service modules, `twitter/auth.py`, `observability/coverage_inventory.py` |
| HTTP transport | `src/tweeter_data_fetcher/twitter/client.py` |
| Request persistence | `src/tweeter_data_fetcher/twitter/request_state.py` |
| GraphQL contracts | `src/tweeter_data_fetcher/twitter/contracts.py` |
| Pagination | `src/tweeter_data_fetcher/twitter/timeline.py` |
| Auth/browser | `src/tweeter_data_fetcher/twitter/auth.py`, `browser.py` |
| Tweet processing | `src/tweeter_data_fetcher/tweets/` |
| Storage | `src/tweeter_data_fetcher/storage/` |
| Observability | `src/tweeter_data_fetcher/observability/` |
| Configuration | `src/tweeter_data_fetcher/configuration.py`, root `config/` |
| Diagnostics | `tools/diagnostics/` |
| Tests | `tests/unit/`, `tests/integration/`, `tests/contract/` |

## Non-Negotiables

- Never commit `config/config.json`, `data/`, diagnostic run output, or `graphify-out/`.
- Prefer root-cause fixes in canonical code.
- Preserve endpoint result dictionaries, reports, state schemas, watermarks, seven processed folders, validation-run isolation, and all `data/` paths.
- Keep Twitter/X request changes evidence-backed by diagnostic captures.
- Do not add database layers, abstract repositories, ports, factories, or new dependencies without a current second implementation.
- After meaningful code changes run `.venv/bin/python -m pytest -q`.
- After architecture changes run `graphify update .`.

## Configuration

Resolution order:

1. Explicit path
2. `TDF_CONFIG`
3. `config/`

Tracked files:

- `config/config.example.json`
- `config/accounts.json`
- `config/searches.json`

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

## Runtime Contracts

Historical and live share `data/historical_live/` and use global two-pass order:

1. Resolve user IDs.
2. Fetch `UserTweets` for every active/due account.
3. Fetch `UserTweetsAndReplies` for every active/due account.
4. Build processed sets.

The rolling cutoff is:

```text
effective_cutoff = min(now - configured_window, floor(fetch_watermark))
```

- Historical floors watermark to day start.
- Live floors watermark to hour start.
- Watermarks advance only after successful endpoint completion.
- Partial/failed runs do not advance watermarks.
- Overlap is expected and deduplicated.

Processed folders remain:

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

```text
data/
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
python tools/diagnostics/verify_contract.py
python tools/diagnostics/probe_txid.py
python tools/diagnostics/probe_sequence.py
python tools/diagnostics/traffic_sniffer.py
```

`FetcherEngine` invokes contract verification directly as a library function. No subprocess launch is allowed for startup verification.

## Tests

```bash
.venv/bin/python -m pytest -q
python -m compileall -q src tests tools
```

Current suite: **108 passed** on July 14, 2026.
