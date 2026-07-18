# TWEETER DATA FETCHER

Twitter/X historical, live, and search pipelines packaged as a Python modular monolith.

## Install

```bash
source .venv/bin/activate
pip install -e .
```

Local authentication is ignored by Git. The canonical path is `twitter_fetcher/config/config.json`.

```bash
cp twitter_fetcher/config/config.example.json twitter_fetcher/config/config.json
tdf-auth --interactive
```

## Commands

```bash
tdf-historical --only elonmusk
tdf-live --account elonmusk --once
tdf-search --once
tdf-coverage --format table
```

The installed `tdf-*` commands call the real modules directly; there is no wrapper-only CLI package.
Each runnable module also supports `python -m ... --help` and starts with a short run/flag guide.

```bash
python -m tweeter_data_fetcher.pipelines.historical.service --help
python -m tweeter_data_fetcher.pipelines.live.service --help
python -m tweeter_data_fetcher.pipelines.search.service --help
python -m tweeter_data_fetcher.observability.coverage_inventory --help
python -m tweeter_data_fetcher.x_api.auth --help
```

All pipelines accept `--validation-run-id <id>` and isolate output under `data/validation/<id>/`.

## Architecture

```text
twitter_fetcher/             current-version parent folder
  config/            tracked templates and account/search definitions
  data/              ignored runtime output (historical_live/, search/)
  src/tweeter_data_fetcher/
    pipelines/       historical, live, and search orchestration
    x_api/           HTTP, request state, contracts, browser/auth, pagination
    processing/      parsing, seven-set operations, rolling windows
    storage/         filesystem, state, exports, StorageManager facade
    observability/   terminal console, file logs, NDJSON events, reports
  tests/             unit, integration, contract, and fixtures
  diagnostics/       evidence-gathering scripts and reports
LEGACY/              read-only archived versions
```

Core entry-point classes: `FetcherEngine`, `APIManager`, `StorageManager`, `TweetSetProcessor`, `RollingWindowEvaluator`, `LiveMonitor`, and `SearchTimelineMonitor`.

## Configuration

Resolution order:

1. Explicit CLI/config path
2. `TDF_CONFIG`
3. Root `twitter_fetcher/config/`

Tracked canonical files:

- `twitter_fetcher/config/config.example.json`
- `twitter_fetcher/config/accounts.json`
- `twitter_fetcher/config/searches.json`

Never commit `twitter_fetcher/config/config.json`.

## Runtime Contracts

Historical and live share `data/historical_live/` and use global two-pass order:

1. Resolve user IDs for active/due accounts.
2. Fetch `UserTweets` for all accounts.
3. Fetch `UserTweetsAndReplies` for all accounts.
4. Build seven processed sets per account.

Let `A = UserTweets` and `B = UserTweetsAndReplies`:

| Folder | Set |
|---|---|
| `1_user_tweets` | `A` |
| `2_user_tweets_and_replies` | `B` |
| `3_intersection` | `A ∩ B` |
| `4_union` | `A ∪ B` |
| `5_a_minus_b` | `A - B` |
| `6_b_minus_a` | `B - A` |
| `7_symmetric_difference` | `A △ B` |

The rolling cutoff remains:

```text
effective_cutoff = min(now - configured_window, floor(fetch_watermark))
```

Query IDs and transaction IDs remain endpoint-specific pools with three-strike rule-out. Rate-limit sleeps remain reset epoch plus safety buffer, bounded by configured maximum (production default 3600 seconds).

Search remains isolated under `data/search/` and never creates historical/live set folders.

## Logging And Diagnosis

Every pipeline uses the same observability path:

- Terminal: tagged Rich output such as `[HIST]`, `[LIVE]`, and `[SEARCH]`.
- File log: rotating `data/<subsystem>/logs/<subsystem>.log` records every console and package logger message with timestamp, level, logger name, and `run_id`.
- Event stream: `data/<subsystem>/logs/events.jsonl` stores structured lifecycle, phase, page, and HTTP-error events.
- HTTP details: `data/<subsystem>/logs/errors/*.json` stores request/response diagnostics; `http_summary.json` aggregates failures by account, endpoint, and status code.
- Reports/state: pipeline reports and watermarks remain under each subsystem's `reports/` and `state/` folders.

Useful diagnosis commands:

```bash
tail -f twitter_fetcher/data/historical_live/logs/historical_live.log
grep '"run_id": "run_..."' twitter_fetcher/data/historical_live/logs/events.jsonl
cat twitter_fetcher/data/historical_live/logs/http_summary.json
```

Secrets may appear in HTTP detail files. Runtime logs remain ignored by Git.

## Diagnostics

```bash
python twitter_fetcher/diagnostics/verify_contract.py
python twitter_fetcher/diagnostics/probe_txid.py
python twitter_fetcher/diagnostics/probe_sequence.py
python twitter_fetcher/diagnostics/traffic_sniffer.py
```

`FetcherEngine` calls the contract verifier as a library function; it no longer launches a diagnostic subprocess. Verification is skipped when no frozen baseline is present.

## Verification

```bash
.venv/bin/python -m pytest -q
python -m compileall -q twitter_fetcher/src twitter_fetcher/tests twitter_fetcher/diagnostics
```

Current baseline: **108 passed** on July 14, 2026.
