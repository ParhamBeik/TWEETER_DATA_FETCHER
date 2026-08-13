# TWEETER DATA FETCHER

Twitter/X historical, live, and search pipelines packaged as a Python modular monolith (v4.1).

All current-version code, config, tests, diagnostics, and runtime data live under `twitter_fetcher/`. The repo root holds that folder plus `LEGACY/` (read-only archive) and project metadata.

## Install

```bash
source .venv/bin/activate
pip install -e .
```

Local authentication is ignored by Git. Canonical path: `twitter_fetcher/config/config.json`.

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
tdf-auth --interactive
```

Installed `tdf-*` scripts call the package modules directly. Each runnable module also supports `python -m ... --help`:

```bash
python -m tweeter_data_fetcher.pipelines.historical.service --help
python -m tweeter_data_fetcher.pipelines.live.service --help
python -m tweeter_data_fetcher.pipelines.search.service --help
python -m tweeter_data_fetcher.observability.coverage_inventory --help
python -m tweeter_data_fetcher.x_api.auth --help
```

Live and historical fetch `UserTweets` only and write `4_union`. Search stays isolated under `twitter_fetcher/data/search/`.

## Layout

```text
twitter_fetcher/
  config/                 tracked templates + accounts/searches
  data/                   ignored runtime output (historical_live/, search/)
  src/tweeter_data_fetcher/
    pipelines/            historical, live, search orchestration
    x_api/                HTTP, auth/browser, contracts, pagination
    processing/           tweet parse + rolling windows
    storage/              StorageManager + filesystem helpers
    observability/        console, file logs, NDJSON events, reports
    paths.py              single source of truth for project paths
  tests/                  unit, integration, contract, fixtures
  diagnostics/            evidence probes and findings reports
LEGACY/                   archived v1–v3 (read-only)
```

Core classes: `FetcherEngine`, `APIManager`, `StorageManager`, `TweetSetProcessor`, `RollingWindowEvaluator`, `LiveMonitor`, `SearchTimelineMonitor`.

## Configuration

Resolution order:

1. Explicit CLI/config path
2. `TDF_CONFIG`
3. `twitter_fetcher/config/`

Tracked files:

- `twitter_fetcher/config/config.example.json`
- `twitter_fetcher/config/accounts.json`
- `twitter_fetcher/config/searches.json`

Never commit `twitter_fetcher/config/config.json`.

## What The Pipelines Do

Historical and live share `twitter_fetcher/data/historical_live/`:

1. Resolve user IDs for active/due accounts
2. Fetch `UserTweets` for all accounts
3. Write `processed/4_union` (the only ingest path)

Rolling cutoff:

```text
effective_cutoff = min(now - configured_window, floor(fetch_watermark))
```

Search stays isolated under `twitter_fetcher/data/search/` and never creates historical/live set folders.

## Logging And Diagnosis

Every pipeline shares one observability path:

- Terminal: tagged Rich output (`[HIST]`, `[LIVE]`, `[SEARCH]`)
- File log: rotating `twitter_fetcher/data/<subsystem>/logs/<subsystem>.log`
- Events: `twitter_fetcher/data/<subsystem>/logs/events.jsonl`
- HTTP details: `logs/errors/*.json` plus `http_summary.json`
- Reports/state: under each subsystem's `reports/` and `state/`

```bash
tail -f twitter_fetcher/data/historical_live/logs/historical_live.log
grep '"run_id": "run_..."' twitter_fetcher/data/historical_live/logs/events.jsonl
cat twitter_fetcher/data/historical_live/logs/http_summary.json
```

Runtime data and HTTP detail files are gitignored. Secrets may appear in HTTP detail files — keep them local.

## Endpoint Contracts, Pacing & Browser Fallback

The pipelines use evidence-backed request contracts and pacing to maintain high reliability and bypass anti-scraping soft-blocks:

| Endpoint | Primary Transport | Referer Header | Pacing & Strategy |
|---|---|---|---|
| `UserTweets` | `APIManager` (`requests`) | `https://x.com/{account}` | Direct GET, short inter-page sleep |
| `SearchTimeline` | `APIManager` + Playwright | `https://x.com/search?...` | Page 1 via HTTP; deeper pages use Playwright |

Detailed request templates, variable definitions, and diagnostic findings are documented in [`ENDPOINT_TEMPLATES_AND_PACING_GUIDE.md`](file:///Users/parham/Downloads/GITHUB_PROJECTS/TWEETER_DATA_FETCHER/twitter_fetcher/diagnostics/reports/ENDPOINT_TEMPLATES_AND_PACING_GUIDE.md).

## Diagnostics

Evidence-gathering scripts (not the pytest suite):

```bash
python twitter_fetcher/diagnostics/verify_contract.py            # config-vs-baseline drift guard (production-wired)
python twitter_fetcher/diagnostics/pagination_test.py             # multi-account UserTweets + SearchTimeline harness
python twitter_fetcher/diagnostics/sniffer.py                     # headful Playwright request capture → sniffer_runs/
```

Curated findings live in `twitter_fetcher/diagnostics/reports/`; the live `sniffer_runs/` capture dir is gitignored.

`FetcherEngine` calls contract verification as a library function (no subprocess). Verification is skipped when no frozen baseline is present.

## Verification

```bash
.venv/bin/python -m pytest -q
python -m compileall -q twitter_fetcher/src twitter_fetcher/tests twitter_fetcher/diagnostics
```

SaaS operator console (`twitter-saas/`): Feed, archive search (`q=`), Trending, Export. One shared X session, one worker.
