# TWEETER DATA FETCHING 4.0

A focused Python toolkit for fetching, monitoring, and exporting tweets from the
Twitter/X GraphQL web API. Three independently-runnable subsystems (historical,
live, search) share one storage layer and one auth/query-id model, plus a
**headful Selenium sniffer** that captures the real browser request shape so the
fetchers stay aligned with what the live site sends.

> For deep, canonical detail see **[AGENTS.md](./AGENTS.md)** (project index,
> architecture notes, request contract, troubleshooting). This README is the
> quick orientation + quickstart.

---

## What it does

| Subsystem | Runner | Captures |
|-----------|--------|----------|
| **Historical** | `historical_scripts/historical_pipeline.py` | Backfills a profile's tweets + replies (`UserTweets`, `UserTweetsAndReplies`). |
| **Live** | `live_scripts/live_pipeline.py` | Continuously polls timelines, dedups seen tweets, detects viral content. |
| **Search** | `search_scripts/search_pipeline.py` | Advanced Search (`SearchTimeline`) by keyword/phrase/account/product. |

All three build on the same shared core:

- `shared/core/twitter_http_client.py` — authed session, rate-limit/backoff, the
  static + session headers, endpoint-specific transaction ID pools with **automatic
  refresh** via headless Playwright when IDs expire.
- `shared/core/pagination_engine.py` — pagination + rolling time-windowing.
- `shared/data_pipeline/storage_manager.py` — raw page saving, the 5 processed
  tweet sets, state files, and subsystem routing.
- `shared/auth/` — cookie setup, query-id refresh, GraphQL sniffer, and automatic
  transaction-id refresh (`auto_refresh.py`).

---

## Unified output layout

Historical and live **share one root** (`data/historical_live/`) — the
`StorageManager` normalizes both `subsystem="historical"` and `subsystem="live"`
to the same path. Search is isolated under `data/search/`.

```
data/
├── historical_live/                 # historical + live share this root
│   ├── raw/
│   │   ├── UserTweets/              #   raw GraphQL response pages (set A source)
│   │   └── UserTweetsAndReplies/    #   raw GraphQL response pages (set B source)
│   ├── processed/
│   │   ├── 1_user_tweets/           #   A — timeline only
│   │   ├── 2_user_tweets_and_replies/  #  B — timeline + replies
│   │   ├── 3_intersection/          #   A ∩ B
│   │   ├── 4_union/                 #   A ∪ B  (deduplicated by tweet id)
│   │   └── 5_replies_only/          #   B − A
│   ├── logs/   reports/   state/    #   fetch logs, run reports, state files
│   └── viral/                       #   snapshots/, reports/  (live-only)
└── search/                          # isolated — never creates the 5 sets
    ├── raw/{slug}/{product}/{batch}/page_{i}.json
    ├── processed/{slug}/{product}/{slug}.{json,txt}
    ├── debug/   logs/   reports/
    └── state/search_state.json
```

**Dedup:** processed sets merge **by tweet id, last-write-wins**
(`StorageManager.merge_processed_items`), so a tweet fetched by either the
historical or live subsystem lands exactly once per set. Search never touches
these folders — it has its own raw/processed/debug layout.

---

## GraphQL sniffer — reverse-engineering the request shape

`shared/auth/graphql_traffic_sniffer.py` is a **headful Selenium** tool that records the
real Twitter/X GraphQL traffic the browser sends — requests **and** responses —
including the per-request JavaScript-generated auth headers (`x-client-transaction-id`,
`query-id`) that a plain HTTP proxy can't see. It is **read-only**: it never
writes `config.json`.

```bash
# Capture a profile's timeline (headful Chrome opens; close it or wait for --timeout)
python -m shared.auth.graphql_traffic_sniffer elonmusk --timeout 120

# Point at any URL (search, with_replies, …)
python -m shared.auth.graphql_traffic_sniffer "https://x.com/search?q=Iran&f=live" --timeout 120
```

Each run emits seven primary artifacts plus one JSON file per selected endpoint
into `sniffer_runs/<jalali_batch>/`:

| Artifact | Purpose |
|---|---|
| `timeline.jsonl` | Arrival-ordered request/response events (URL, method, full headers, body, status). |
| `timeline.html` | Human waterfall; surfaces `x-client-transaction-id`, `x-csrf-token`, `x-twitter-auth-type`, rate-limit headers. |
| `clean_timeline.jsonl` | Redacted, paired request/response summaries for selected endpoints. |
| `contract.json` | Per-endpoint structured contract (query-id, url template, variables/features/fieldToggles, dynamic header notes, rate-limit sample). |
| `endpoint_contracts/*.json` | Endpoint-specific templates and config comparisons. |
| `diagnostics.md` | Capture counts, status/config checks, and failure guidance. |
| `playbook.md` | Paste-ready endpoint/header reference with explicit evidence boundaries. |

**On `x-client-transaction-id`:** the captured browser supplied a distinct
94-character value on each of 23 requests. The capture does not reveal the
generation algorithm or prove how strictly X validates the value. The Python
runtime uses one fallback value per session; a controlled July 2026 probe proved
that it worked for `UserByScreenName` and two `UserTweets` pages in that session,
but this is bounded evidence rather than a permanent protocol guarantee.

> `sniffer_runs/` contains **live credentials** (auth tokens/cookies in headers)
> and is gitignored. `playbook.md` masks secret values; raw captures must not be
> shared. Requires Chrome + chromedriver (selenium-manager fetches the matching
> driver automatically; if blocked, `brew install --cask chromedriver`).

### Browser-verified runner contract (`1405-04-14_15-20`)

This capture contains 23 requests and 23 responses over 117.71 seconds. Every
browser response was HTTP 200 with non-null GraphQL data and no top-level
`errors`. The runner endpoints were:

| Endpoint | Query ID | Initial variables | Pagination |
|---|---|---|---|
| `UserByScreenName` | `2qvSHpkWTMS9i0zJAwDNiA` | `screen_name`, `withGrokTranslatedBio: true` | None |
| `UserTweets` | `hr4gzZONlq23okjU8fIe_A` | `userId`, `count: 20`, promoted content, quick-promote fields, voice | Add the preceding bottom cursor unchanged |
| `UserTweetsAndReplies` | `FIFgycIi-CNJcV0R-135Uw` | `userId`, `count: 20`, promoted content, community, voice | Add the preceding bottom cursor unchanged |
| `SearchTimeline` | `Bcw3RzK-PatNAmbnw54hFw` | `rawQuery`, `count: 20`, `querySource`, `product`, translated bio, quick-promote false | Add the preceding bottom cursor unchanged |

`UserTweets` and `UserTweetsAndReplies` send
`fieldToggles={"withArticlePlainText":false}`; `SearchTimeline` omits field
toggles. All three timeline endpoints used the captured 39-feature map stored in
`shared/config/config.json`.

Safe request sequence:

1. Resolve the current user ID with `UserByScreenName`.
2. Send an initial timeline request without a cursor.
3. Extract only the `cursorType: Bottom` value from `TimelineAddEntries`.
4. Send it unchanged on the next request. Never invent, truncate, or carry a
   cursor between endpoints, users, queries, or sessions.
5. Deduplicate by tweet ID. Adjacent captured profile pages repeated three IDs.
6. Check HTTP status, GraphQL `errors`, and non-null `data` before parsing.

Observed headers indicated 15-minute rate windows: limit 50 for `UserTweets`
and `SearchTimeline`, 150 for `UserByScreenName`, and 500 for
`UserTweetsAndReplies`. These values describe one session, not guaranteed global
limits; always obey the current response headers.

### 4xx decision table

| Result | First checks | Safe action |
|---|---|---|
| `400` | Query ID, exact variables/features/field toggles, compact JSON encoding | Compare with the captured contract; do not retry an unchanged malformed request. |
| `401`/`403` | `auth_token`, `ct0`, Bearer header, `x-csrf-token == ct0` | Refresh authentication without changing the payload. |
| Initial `404` | Query ID and route/session context | Treat as rejection. A controlled probe observed this for replies despite payload parity. |
| Cursor `404` | Cursor provenance and freshness | Preserve collected pages, stop the chain, and later start from a fresh initial request. |
| `429` | Remaining count and reset epoch | Sleep through reset plus a safety buffer, then retry the same request. |
| `200` with errors/null data | GraphQL `errors` and endpoint data path | Treat as failure; HTTP 200 alone is insufficient. |

The complete 16-endpoint evidence record is in `AGENTS.md` and the run-local
`playbook.md`/`diagnostics.md`.

### Runtime validation mode

All runners can write canary output to an isolated tree under
`data/validation/<run_id>/` so stale production state cannot satisfy a current
run:

```bash
python historical_scripts/historical_pipeline.py --only <account> --validation-run-id canary_001
python live_scripts/live_pipeline.py --account <account> --once --validation-run-id canary_001
python search_scripts/search_pipeline.py --once --only "<search name>" --validation-run-id canary_001
```

Endpoint reports use `verified_http` when browser bootstrap plus HTTP
pagination validates, `verified_browser_fallback` when targeted browser capture
is needed, and `failed`/`unverified` otherwise. A workable validation batch
requires valid target pages for `UserTweets`, `UserTweetsAndReplies`, and
`SearchTimeline`; HTTP 200 alone is not accepted if GraphQL `errors`, null data,
or an unsupported timeline shape appears.

---

## Auth & query-id refresh

Twitter rotates GraphQL `query-id`s periodically. The fetchers read them from
`api_config` in `shared/config/config.json`.

```bash
# 1) Harvest fresh cookies (auth_token, ct0, …) into config.json
python shared/auth/auto_refresh.py --interactive

# 2) After a sniffer run, apply newly captured query-ids into config.json
python shared/auth/auto_refresh.py --interactive
```

`auto_refresh.py` owns the config **write** step (atomic save with backup);
the sniffer only observes. If cookies expire you'll see persistent 401/403 —
re-run `auto_refresh.py --interactive`.

### Automatic Transaction ID Refresh

The system uses **endpoint-specific transaction IDs** (94-char base64 strings) captured
from real browser sessions. When all transaction IDs for an endpoint become stale
(after 3 consecutive 404s), the system automatically:

1. Launches headless Playwright
2. Visits profile → replies → search pages with scrolling
3. Intercepts GraphQL requests and extracts fresh tx-ids per endpoint
4. Updates `config.json` and retries the request

**This happens automatically—no manual intervention needed.** Each auto-refresh adds
10-30 seconds latency but maintains uninterrupted operation.

To manually refresh transaction IDs:
```bash
python shared/auth/auto_refresh.py --interactive  # Full refresh (cookies + query-ids + tx-ids)
```

**Troubleshooting:** If you see persistent 404s on UserTweetsAndReplies or SearchTimeline
after cookies are valid, the auto-refresh system will trigger within 3 attempts. Monitor
logs for `[WARNING] All transaction IDs stale` messages.

> `config.json` holds secrets and is gitignored. `sniffer_runs/` likewise.

---

## Quickstart

```bash
cd "TWEETER DATA FETCHING 4.0"

# --- one-time setup ---------------------------------------------------------
pip3 install pytz jdatetime rich
pip3 install selenium playwright        # only for sniffer / update_query_ids
playwright install chromium             # only for update_query_ids

# Configure auth (creates shared/config/config.json)
python shared/auth/auto_refresh.py --interactive

# --- run a subsystem --------------------------------------------------------
python historical_scripts/historical_pipeline.py                 # backfill timelines
python live_scripts/live_pipeline.py                             # continuous live monitor
python search_scripts/search_pipeline.py --once                  # one search pass
python search_scripts/search_pipeline.py --once --only "My Search"

# --- capture the live request shape ----------------------------------------
python -m shared.auth.graphql_traffic_sniffer elonmusk --timeout 120
```

Common runner flags:

| Runner | Flags |
|--------|-------|
| historical | `--only <user>` (repeatable), `--no-user-tweets`, `--no-with-replies` |
| live | `--account <user>` (repeatable), `--once`, `--check-interval <s>` |
| search | `--only "<name>"` (repeatable), `--once`, `--check-interval <s>` |

Add accounts in `shared/config/account_tiers.py`; define searches in
`shared/config/search_config.json`.

---

## Dependencies

| Package | Required by |
|---------|-------------|
| Python 3.11+ | everything |
| `pytz` | all (timezone, `Asia/Tehran` default) |
| `jdatetime` | optional — Jalali batch naming |
| `rich` | optional — terminal UI |
| `selenium` | the GraphQL sniffer (+ Chrome/chromedriver) |
| `playwright` | `shared/auth/auto_refresh.py` only |

---

## Project layout (source map)

See [`structure.txt`](./structure.txt) for the full source-module map and
[`AGENTS.md`](./AGENTS.md) for architecture, the request contract, state
management, and troubleshooting.


---

## Implementation Notes (V4 Consolidation)
The V4 codebase has been streamlined for better maintainability and clarity:
1. **Removed Wrappers**: Legacy runner scripts (e.g., `live_runner.py`) were deleted.
2. **Consolidated the Core**: Tiny utility files were merged. `live_state.py` and `detect_viral.py` became `live_utils.py`. `tweet_sets.py`, `date_windows.py`, and `graphql_shapes.py` became `tweet_processing_utils.py`. The exporters were integrated into `storage_manager.py`.
3. **Renamed for Clarity**: Scripts were renamed to explicitly reflect their roles (e.g., `fetch_history.py` became `historical_pipeline.py`, `timeline_fetcher.py` became `pagination_engine.py`).
4. **Probing & Testing**: Added robust probing sequences (`probe_sequence.py`, `probe_txid.py`) and test suites (`test_graphql_contracts.py`) within the `tools/` and `tests/` directories to validate the GraphQL API behavior in real-time.
