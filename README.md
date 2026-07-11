# TWEETER DATA FETCHER

Python 3.11 toolkit for fetching Twitter/X GraphQL data through the web API contract observed from real browser sessions. The current project lives at repo root under `src/`; old v1-v3 code is archived under `LEGACY/`.

## Current State

Latest project state: July 2026.

- Historical and live now share one rolling-window mechanism, one storage root, and one fetcher engine.
- Historical and live both run profile endpoints in global two-pass order: `UserTweets` for every due account, then `UserTweetsAndReplies` for every due account.
- Rolling windows are timestamp-granular and watermark-backed, so a mid-day or mid-hour successful fetch cannot create a permanent gap.
- Processed profile output now has 7 sets: `A`, `B`, `A ∩ B`, `A ∪ B`, `A - B`, `B - A`, and `A △ B`.
- Query IDs and transaction IDs are endpoint-specific pools with 3-strike rule-out before a value is skipped.
- Rate-limit sleeps use response reset epochs, with a 3600 second cap.
- `src/shared/config/config.json` is local-only and ignored because it contains live cookies/tokens. Use `src/shared/config/config.example.json` as the safe template.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest

cp src/shared/config/config.example.json src/shared/config/config.json
python src/shared/auth/auto_refresh.py --interactive

python -m src.pipelines.historical.fetch_historical --only elonmusk
python -m src.pipelines.live.monitor_live --account elonmusk --once
python -m src.pipelines.search.search_timeline --once
```

Run tests:

```bash
.venv/bin/python -m pytest -q
```

## Main Entry Points

| Area | Module | Purpose |
|---|---|---|
| Historical | `src/pipelines/historical/fetch_historical.py` | Backfill profile timeline and replies for configured accounts. |
| Live | `src/pipelines/live/monitor_live.py` | Poll due accounts, update shared processed sets, maintain live seen-tweet and viral state. |
| Search | `src/pipelines/search/search_timeline.py` | Poll configured `SearchTimeline` queries into isolated search storage. |
| Diagnostics | `tests/diagnostics/*.py`, `tools/*.py` | Probe endpoint health, verify request contract, compare parity. |

## Source Map

| Path | Role |
|---|---|
| `src/shared/core/twitter_http_client.py` | `APIManager`: HTTP session, auth headers, tx/query-id pools, rate-limit state, retry helpers. |
| `src/shared/core/pagination_engine.py` | `FetcherEngine`: user lookup, timeline URL construction, pagination, window cutoff stop, raw page persistence. |
| `src/shared/core/tweet_processing_utils.py` | GraphQL contracts, timestamp parsing, rolling-window evaluator, tweet extraction, set math. |
| `src/shared/data_pipeline/storage_manager.py` | Raw/processed/report/state file I/O and historical/live/search storage routing. |
| `src/shared/auth/browser_context.py` | Playwright/browser context bootstrap used by fetchers for warm session state. |
| `src/shared/auth/auto_refresh.py` | Playwright session refresh; captures cookies, tx-id pools, query-id pools and writes local config. |
| `src/shared/config/account_tiers.py` | Account list and priority policy defaults. |
| `src/shared/config/search_config.json` | Search query definitions. |
| `src/pipelines/live/utils.py` | Live state, seen tweet index, viral snapshots and reports. |

## Data Layout

Runtime data is ignored and written under `data/`.

```text
data/
  historical_live/
    raw/
      UserTweets/{account}/{batch}/page_N.json
      UserTweetsAndReplies/{account}/{batch}/page_N.json
    processed/
      1_user_tweets/
      2_user_tweets_and_replies/
      3_intersection/
      4_union/
      5_a_minus_b/
      6_b_minus_a/
      7_symmetric_difference/
    reports/
    state/
      sync_state.json
      live_state.json
      seen_tweets.json
      tx_id_state.json
      query_id_state.json
      rate_limits.json
    viral/
  search/
    raw/{search_slug}/{product}/{batch}/page_N.json
    processed/{search_slug}/{product}/
    debug/
    reports/
    state/search_state.json
```

Historical and live intentionally share `data/historical_live/`; search is isolated and must never create profile set folders.

## Rolling Window Model

Each successful account+endpoint fetch stores `fetch_watermark` in `sync_state.json`. The next cutoff is:

```text
min(now - configured_window, floor(fetch_watermark))
```

Historical floors the watermark to day start. Live floors it to hour start. The overlap is intentional and is absorbed by tweet ID deduplication.

Default priority windows:

| Priority | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---:|---:|---:|---:|---:|---:|---:|
| live hours | 24 | 20 | 16 | 12 | 9 | 6 | 3 |
| historical days | 7 | 6 | 5 | 4 | 3 | 2 | 2 |

## Processed Sets

Let `A = UserTweets` and `B = UserTweetsAndReplies`.

| Folder | Meaning |
|---|---|
| `1_user_tweets` | `A` |
| `2_user_tweets_and_replies` | `B` |
| `3_intersection` | `A ∩ B` |
| `4_union` | `A ∪ B` |
| `5_a_minus_b` | `A - B` |
| `6_b_minus_a` | `B - A` |
| `7_symmetric_difference` | `A △ B` |

The dedup key is `author_id:tweet_id` when author ID exists, otherwise tweet ID.

## Auth, Query IDs, Transaction IDs

`APIManager` reads local `src/shared/config/config.json`.

- `api_cookies.ct0` must match the `x-csrf-token` header.
- `api_auth.bearer_token` is the public web bearer token.
- `query_ids_by_endpoint` provides endpoint query-id pools.
- `real_transaction_ids_by_endpoint` provides endpoint tx-id pools captured from browser requests.
- `tx_id_state.json` and `query_id_state.json` rule out params only after 3 consecutive failures.

Manual refresh:

```bash
python src/shared/auth/auto_refresh.py --interactive
```

GraphQL capture/probe tools are diagnostic. They can contain live credential material in output, so capture output stays ignored.

## Graphify

A graphify graph lives in `graphify-out/` and is ignored because it is generated. Use it for architecture questions:

```bash
graphify query "How does FetcherEngine write watermarks?"
graphify path "APIManager" "StorageManager"
graphify update .
```

## Verification

Current green check:

```bash
.venv/bin/python -m pytest -q
# 49 passed
```

Useful targeted checks:

```bash
.venv/bin/python -m pytest tests/unit/test_unified_historical_live_plan.py -q
python -m compileall -q src tests
```
