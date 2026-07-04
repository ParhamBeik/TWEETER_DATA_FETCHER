# TWEETER DATA FETCHING 4.0 — Project Index & Guide

## Quick Navigation

| Component | Description | Main File(s) |
|-----------|-------------|--------------|
| **Historical** | Fetches tweets from historical timelines | `historical_scripts/historical_runner.py` |
| **Live** | Monitors live tweets with viral detection | `live_scripts/live_runner.py`, `live_scripts/live_storage.py` |
| **Search** | Advanced search timeline monitoring | `search_scripts/search_runner.py` |
| **Shared Core** | API manager, fetcher engine, utilities | `shared/core/*` |
| **Shared Storage** | Data persistence and state management | `shared/data_pipeline/storage_manager.py` |
| **Shared Config** | API keys, endpoints, tier configs | `shared/config/*` |
| **Auth & Sniffer** | Cookie setup, query-id refresh, traffic capture | `shared/auth/*` (incl. `graphql_sniffer.py`, `session_updater.py`) |

> **Knowledge graph:** a graphify graph lives at `../graphify-out/` (repo root). For codebase questions run
> `graphify query "<question>"`, `graphify path "<A>" "<B>"` for relationships, or `graphify explain "<concept>"`.
> After modifying code, refresh it from the repo root with `graphify update .` (AST-only, no API cost).

## Data Storage Layout

```
data/
├── historical_live/          # Historical + Live data (shared root)
│   ├── raw/
│   │   ├── UserTweets/
│   │   └── UserTweetsAndReplies/
│   ├── processed/
│   │   ├── 1_user_tweets/
│   │   ├── 2_user_tweets_and_replies/
│   │   ├── 3_intersection/
│   │   ├── 4_union/
│   │   └── 5_replies_only/
│   ├── reports/
│   ├── state/                # Contains sync_state.json (historical/live only)
│   └── viral/
│       ├── snapshots/
│       └── reports/
└── search/                   # Search data (isolated from historical)
    ├── raw/
    │   └── {search_slug}/{product}/{jalali_batch}/
    │       └── page_{i}.json
    ├── processed/
    │   └── {search_slug}/{product}/
    │       ├── {slug}.json
    │       └── {slug}.txt
    ├── debug/
    │   └── {search_slug}/{product}/
    │       └── {slug}__debug_first_page_{name}.json
    ├── reports/
    └── state/
        └── search_state.json
```

> **Note:** Search data is isolated. It does NOT create `1_user_tweets/`, `2_user_tweets_and_replies/`, etc. These folders belong exclusively to historical/live processing.

> **Unified root (historical + live):** Both `subsystem="historical"` and `subsystem="live"` collapse to the **same** `data/historical_live/` root (`storage_manager.py` subsystem normalization: `live`/`historical` → `historical_live`). They write the same `raw/UserTweets/`, `raw/UserTweetsAndReplies/`, and the same five `processed/` set folders. Tweet-level dedup is **by tweet id, last-write-wins** (`merge_processed_items`), so a tweet fetched by either subsystem lands exactly once per set. Live-only additive artifacts (`state/live_state.json`, `state/seen_tweets.json`, `state/snapshot_index.json`, `viral/`) never collide with historical.

> **Sniffer runs:** Headful-capture output from `shared/auth/graphql_sniffer.py` lands in `sniffer_runs/<jalali_batch>/` at the v4 root (gitignored — contains full request/response headers incl. `authorization` and `ct0`). See [Sniffer-Derived GraphQL Request Contract](#sniffer-derived-graphql-request-contract).

---

## Common Workflows

### Adding a New Twitter Account (Historical / Live)

1. Open `shared/config/tier_config.py`.
2. Find the tier category for your account (`tier_1`, `tier_2`, etc.).
3. Add an entry as a dict:
   ```python
   {"username": "new_account", "polling_priority": 1}
   ```
   - `polling_priority` 1-7 determines the polling interval and safety caps.
4. Save the file. The historical and live runners will pick it up automatically.

### Adding or Editing a Search Query

1. Open `shared/config/search_config.json`.
2. Each entry is a search definition. To add a new one:
   ```json
   {
     "name": "My New Search",
     "enabled": true,
     "product": "Latest",
     "preserve_exact_query": false,
     "raw_query": "your search terms here",
     "polling_priority": 3,
     "rolling_hours": 24,
     "poll_interval_seconds": 600,
     "include_keywords": ["keyword1", "keyword2"],
     "exclude_keywords": ["spam", "bot"],
     "exact_phrases": ["exact phrase to match"],
     "from_account": "optional_username",
     "to_account": "optional_username",
     "hashtags": ["#hashtag"]
   }
   ```
3. `preserve_exact_query: true` with `exact_query` field skips keyword parsing and uses the raw string as-is.
4. `product` must be one of: `Top`, `Latest`, `Media`, `People`.
5. Save and the search runner picks it up on its next cycle.

### Updating API Cookies

1. Open Twitter/X in your browser and log in.
2. Open DevTools (F12) → Application tab → Cookies for `x.com`.
3. Export these cookie values: `auth_token`, `ct0`, `guest_id`, `kdt`, `twid`.
4. Run:
   ```bash
   python shared/auth/setup_api_cookies.py
   ```
   Or manually edit `shared/config/config.json` under the `api_cookies` key.
5. **Important:** Cookies expire. If you see persistent 401/403 errors, refresh them.

### Capturing Live Traffic (GraphQL Sniffer)

`shared/auth/graphql_sniffer.py` is a **headful Selenium** capture tool that records the **real** Twitter/X
GraphQL request shape the browser sends — including the per-request JavaScript-generated auth headers
(`x-client-transaction-id`, `query-id`) — so you can keep the fetchers aligned with what the live site
actually does. It is **read-only/diagnostic**: it never writes `config.json`. Applying captured query-ids is
[`session_updater.py`](#auth--sniffer)'s job.

```bash
# Capture a profile's timeline traffic (headful Chrome opens; close it or wait for --timeout)
python -m shared.auth.graphql_sniffer elonmusk --timeout 120

# Point it at an arbitrary URL (search, with_replies, etc.)
python -m shared.auth.graphql_sniffer "https://x.com/search?q=Iran&f=live" --timeout 120

# Override the output directory (default: sniffer_runs/<jalali_batch>/)
python -m shared.auth.graphql_sniffer elonmusk --output-dir ./my_capture
```

Each run emits **four artifacts** into `sniffer_runs/<jalali_batch>/`:

| Artifact | What it is |
|---|---|
| `timeline.jsonl` | Arrival-ordered request/response events (URL, method, full headers, body, status). |
| `timeline.html` | Human-readable waterfall; columns surface `x-client-transaction-id`, `x-csrf-token`, `x-twitter-auth-type`, rate-limit headers. |
| `contract.json` | Per-endpoint structured contract: `{query_id, method, url_template, variables_sample, features, fieldToggles, request_headers_seen, dynamic header notes, referer_sample, response_status_codes, sample_rate_limit}`. |
| `playbook.md` | Paste-ready markdown for this file/README: endpoint table, per-endpoint detail, static-vs-dynamic header split, and the `x-client-transaction-id` algorithm note. |

**How capture works:** a small fetch/XHR interceptor is injected into the page via Chrome DevTools
(`Page.addScriptToEvaluateOnNewDocument`) **before** any page script runs, so it sees exactly the headers
the site's own JS sets — including ones a plain HTTP proxy would not synthesize. Captured tokens are surfaced
from the recorded headers; nothing is replayed or generated.

**Keeping the contract current:** when Twitter rotates query-ids, run the sniffer, then apply the new ids with
`python shared/auth/session_updater.py` (or paste them into `api_config` in `shared/config/config.json`). The
sniffer deliberately stops at observation — it does not auto-write config.

**Caveat:** requires a working Chrome + chromedriver. Selenium's built-in `selenium-manager` normally fetches
the matching driver; if that download is blocked (e.g. offline sandbox), install it manually:
`brew install --cask chromedriver` (macOS) or download from chrome-for-testing. The captured run contains
**live credentials** (auth tokens/cookies in headers) — `sniffer_runs/` is gitignored; do not share raw
captures. The `playbook.md` masks secret values.

---

## Troubleshooting & Debugging

### Rate Limiting (HTTP 429)

- All three modules implement exponential backoff on 429 responses.
- `APIManager.rate_limit_sleep_seconds()` parses `x-rate-limit-reset` headers.
- Default retry: 3 attempts with jitter.
- If rate limits persist, reduce `polling_priority` (lower number = more aggressive = higher rate limit risk) or increase `poll_interval_seconds` in config.

### Authentication Failures (401 / 403)

- **Cause:** Expired cookies or revoked session.
- **Fix:** Update cookies per "Updating API Cookies" workflow above.
- Check `auth_token` and `ct0` in `shared/config/config.json`.

### Cursor Exhaustion (HTTP 404)

- **Normal:** Happens when you reach the end of available tweets.
- **Partial:** Some pages fetched before a 404 — the runner marks it as `partial_cursor_404` and saves what it has.
- **First-page 404:** Likely an invalid query or account does not exist.

### Empty Pages / No Tweets

- Check `rolling_hours` setting — a very narrow window may yield nothing.
- Verify `raw_query` syntax is correct for the Twitter Search API.
- Look at debug output: `data/search/debug/{slug}/{product}/{slug}__debug_first_page_*.json` shows entry type counts and skipped entries.

### Logs

| Log Location | Contents |
|---|---|
| `data/historical_live/logs/` | Historical fetch events, endpoint health, state updates |
| `data/search/logs/` | Search fetch events and pagination details |
| `data/historical_live/reports/` | Run report JSON files (per-run summaries) |
| `data/search/reports/` | Search run report JSON files |

---

## Data Schema Reference

### Tweet Object (Processed)

Each tweet in processed JSON files contains these fields:

| Field | Type | Source | Description |
|-------|------|--------|-------------|
| `id` | str | `rest_id` or `id` | Twitter tweet ID |
| `text` | str | `legacy.full_text` | Tweet text content |
| `created_at` | str | `legacy.created_at` | Twitter timestamp (RFC 2822) |
| `raw_timestamp` | str | Parsed | ISO format timestamp for windowing |
| `likes` | int | `legacy.favorite_count` | Like count |
| `retweets` | int | `legacy.retweet_count` | Retweet count |
| `replies` | int | `legacy.reply_count` | Reply count |
| `quotes` | int | `legacy.quote_count` | Quote count |
| `bookmarks` | int | `legacy.bookmark_count` | Bookmark count |
| `views` | int | `views.count` | View count |
| `type` | str | Computed | `tweet`, `retweet`, `reply`, or `quote` |
| `source_endpoint` | str | Added by pipeline | Which endpoint produced this |

### Raw Page Format

Raw GraphQL response pages are saved as-is from Twitter's API. Structure:

```json
{
  "data": {
    "user": { ... },           // for UserTweets / UserTweetsAndReplies
    "search_by_raw_query": {   // for SearchTimeline
      "search_timeline": {
        "timeline": {
          "instructions": [ ... ]
        }
      }
    }
  },
  "_attempts": 1,
  "_status": 200,
  "_error_samples": []
}
```

### Processed Output Files

| File | Description |
|------|-------------|
| `1_user_tweets.json` | Tweets from the user's timeline only |
| `2_user_tweets_and_replies.json` | Tweets + replies from the user |
| `3_intersection.json` | Tweets that appear in BOTH A and B |
| `4_union.json` | All tweets from A ∪ B (deduplicated by ID) |
| `5_replies_only.json` | Replies in B that are NOT in A (B - A) |

---

## Architecture Notes

### Three Isolated Subsystems

| Subsystem | State File | Storage Root |
|-----------|-----------|--------------|
| **Historical** | `sync_state.json` | `data/historical_live/` |
| **Live** | `live_state.json`, `seen_tweets.json` | `data/historical_live/` |
| **Search** | `search_state.json` | `data/search/` |

Each subsystem is independently runnable. They do not share state files or interfere with each other.

### The `subsystem` Parameter in `StorageManager`

- `subsystem="historical"` or `subsystem="live"` → merges into `historical_live` storage root.
- `subsystem="search"` → uses `data/search/` as its root.
- This merging means historical and live share the same base directory but maintain separate state files via their own managers.

### Search Module Isolation (Refactored June 2026)

The search subsystem uses `StorageManager` with two safety flags:

```python
self.storage = StorageManager(
    base_dir=self.project_root,
    subsystem="search",
    create_folders=False,       # Don't create the 5 historical processed folders
    manage_sync_state=False,    # Don't touch sync_state.json
)
```

- `save_search_result_page()` writes to `data/search/raw/{slug}/{product}/{batch}/page_{i}.json`.
- `save_raw_page()` is the historical/live method — search should **never** call it.
- `_ensure_base_dirs()` is skipped entirely, so `1_user_tweets/` etc. are never created.

### Live Module Isolation

`LiveStorageManager` (`live_scripts/live_storage.py`) manages its own state files independently:
- `live_state.json` — account polling state (last cursor, status)
- `seen_tweets.json` — deduplication set
- `snapshot_index.json` — viral snapshot tracking
- It wraps `StorageManager` internally for shared I/O (text export, batch naming) but owns all state.

---

## Environment & Dependencies

### Prerequisites

| Requirement | Details |
|-------------|---------|
| Python | 3.11+ (tested on 3.11) |
| `pytz` | Timezone handling (Asia/Tehran is the default) |
| `jdatetime` | Jalali calendar conversion (optional, fallback is built-in) |
| `rich` | Optional — provides terminal UI formatting |
| `selenium` | Required only by the GraphQL sniffer (`shared/auth/graphql_sniffer.py`) for headful capture |
| `playwright` | Required only by `shared/auth/session_updater.py` (query-id refresh). Install browsers with `playwright install chromium`. |

Install dependencies:
```bash
pip3 install pytz
pip3 install jdatetime    # optional, improves Jalali formatting
pip3 install rich          # optional, improves terminal output
pip3 install selenium      # only for the GraphQL sniffer
pip3 install playwright    # only for session_updater (query-id refresh)
```

### Virtual Environment

No virtualenv is configured by default. If you want one:
```bash
cd "/Users/parham/Downloads/GITHUB_PROJECTS/TWEETER_DATA_FETCHER/TWEETER DATA FETCHING 4.0"
python3 -m venv .venv
source .venv/bin/activate
pip install pytz jdatetime rich
```

### Timezone

All timestamps use `Asia/Tehran` by default. Jalali (Persian) dates are used for batch naming and run IDs.

---

## Code Conventions & Patterns

### Naming Patterns

- **Slugs:** Generated via `StorageManager._normalize_username()` — strips `@`, removes special chars, lowercase.
- **Batch names:** Jalali date format `YYYY-MM-DD` via `StorageManager._jalali_batch_name()`.
- **Run IDs:** `run_YYYY-MM-DD_HH-MM-SS` using Jalali time.

### Error Handling Pattern

All network modules use a consistent pattern:
```python
{
    "_failure": "error_category",      # descriptive error reason
    "_status": 429,                    # HTTP status code (or None)
    "_attempts": 3,                    # total attempts made
    "_error_samples": [ ... ],         # last 5 error details
}
```

Common `_failure` values:
| Value | Meaning |
|-------|---------|
| `failed_initial_auth` | First request returned 401/403 |
| `failed_initial_rate_limit` | First request returned 429 |
| `partial_cursor_404` | Some pages fetched, then cursor 404'd |
| `partial_rate_limited` | Rate limit persisted after pages |
| `success_search_window_crossed` | Search found tweets outside the time window |
| `repeated_cursor_history` | Cursor loop detected in pagination |

### Fetcher Engine Configuration Flow

```
config.json
    → APIManager (loads cookies, tokens, query IDs)
        → FetcherEngine (creates APIManager, sets up session, pagination caps)
            → StorageManager (creates via FetcherEngine, gets base_dir and subsystem)
```

Each runner instantiates `FetcherEngine` → `APIManager` → `StorageManager` in that order. Config is read once from `shared/config/config.json`.

### Query ID Resolution

Endpoint-specific query IDs (e.g., `UserTweets`, `SearchTimeline`) are looked up via `APIManager.get_query_id(endpoint)` from the `api_config` section of `config.json`. If missing, the runner raises `RuntimeError`.

### Core Rule: Strict YAGNI

Use the simplest correct implementation for the current Twitter/X request patterns:

- Do not add abstractions, layers, interfaces, config knobs, or generalized designs unless they are required by the current working code.
- Prefer direct endpoint-specific code when only one caller needs the behavior.
- Do not add transaction-ID pools, rotation systems, or guessed anti-bot mechanics unless captured logs prove they are required and the runtime uses them now.
- Do not generalize untargeted endpoints while fixing `UserTweets`, `UserTweetsAndReplies`, or `SearchTimeline`.
- If existing complexity can be replaced by a simpler structure without losing behavior, propose or make that simplification.
- Store only request-shape data that the code actually reads now; document observations separately instead of adding unused config.

### Sniffer-Derived GraphQL Request Contract (July 2026)

`shared/auth/graphql_sniffer.py` (headful Selenium + CDP-injected interceptor) captures the **real**
browser request shape and emits four artifacts per run — `timeline.jsonl`, `timeline.html`,
`contract.json`, and a paste-ready `playbook.md` into `sniffer_runs/<jalali_batch>/` (see
[Capturing Live Traffic](#capturing-live-traffic-graphql-sniffer)). The table below is the contract the
sniffer is designed to verify; the query-ids are the **last values seen live** and must be re-captured
when Twitter rotates them. The project state is intentionally minimal: keep the query-ids and exact
payload shape aligned with the live site, but do not add generalized request builders. Keep
`shared/config/config.json`, `shared/core/api_manager.py`, `shared/core/fetcher_engine.py`, and
`search_scripts/search_runner.py` aligned with these invariants.

| Endpoint | Query ID | Referer Pattern | Variables |
|---|---|---|---|
| `UserTweets` | `hr4gzZONlq23okjU8fIe_A` | `https://x.com/{username}` | `userId`, `count: 20`, optional `cursor`, `includePromotedContent: true`, `withQuickPromoteEligibilityTweetFields: true`, `withVoice: true` |
| `UserTweetsAndReplies` | `FIFgycIi-CNJcV0R-135Uw` | `https://x.com/{username}/with_replies` | `userId`, `count: 20`, optional `cursor`, `includePromotedContent: true`, `withCommunity: true`, `withVoice: true` |
| `SearchTimeline` | `Bcw3RzK-PatNAmbnw54hFw` | `https://x.com/search?...&src=typed_query` or trend URL | `rawQuery`, `count: 20`, optional `cursor`, `querySource`, `product`, `withGrokTranslatedBio: true`, `withQuickPromoteEligibilityTweetFields: false` |

Common request details:
- Method is `GET` against `https://x.com/i/api/graphql/{query_id}/{endpoint}`.
- Query string includes compact JSON `variables` and `features`.
- `UserTweets` and `UserTweetsAndReplies` include `fieldToggles={"withArticlePlainText":false}`; `SearchTimeline` omits `fieldToggles`.
- Shared feature flags match the sniffer payloads, including `post_ctas_fetch_enabled: false`.
- `UserByScreenName` remains on its existing simple lookup path unless that endpoint is explicitly being refreshed from a sniffer run.

**Static vs. per-request dynamic headers** (the split the `playbook.md` calls out):

| Header | Class | How to source it |
|---|---|---|
| `authorization` | **Static** | Public web Bearer token — constant across all web clients; same value for every request/session. |
| `x-twitter-auth-type: OAuth2Session` | **Static** | Constant while authenticated. |
| `x-csrf-token` | **Session-bound** | Equals the `ct0` cookie value for this session. Set by `APIManager` from `api_cookies.ct0`. |
| `x-twitter-active-user: yes`, `x-twitter-client-language: en`, `content-type`, `user-agent`, `sec-ch-ua*` | **Static** | Constant; set by `APIManager`. |
| `referer` | **Page-dependent** | Derived from the endpoint / page (`x.com/{user}`, `…/with_replies`, `…/search?…`). |
| `x-client-transaction-id` | **Dynamic (per-request)** | Generated client-side by the page JS — see algorithm note below. |

**`x-client-transaction-id` algorithm (observed, documented — not reimplemented):**

1. The value is **generated in the browser per request** by Twitter's web JS, derived from the request
   method + path plus an animation-frame / timing-derived seed. It **rotates every request**; it is **not**
   a fixed pool of valid ids and is **not** validated strictly server-side.
2. The project does **not** reimplement this generator in Python. `api_headers.x-client-transaction-id`
   may be left blank; `APIManager` synthesizes a stable **fallback session transaction id** so requests
   still carry the header. This is sufficient for the endpoints the fetchers use today.
3. **Do not** hardcode captured transaction ids into a pool/rotation list — that contradicts (1) and the
   [Strict YAGNI](#core-rule-strict-yagni) rule. Capture is for understanding, not replay.
4. If a future anti-bot change makes a *real* rotating tx-id mandatory, the source of truth is the page JS;
   the sniffer (`timeline.jsonl` + `contract.json`) is how you'd confirm the new shape before building
   anything. Until then, the fallback is correct.

**Keeping the contract current:** run `python -m shared.auth.graphql_sniffer <profile> --timeout 120`,
read the emitted `contract.json` / `playbook.md`, then apply refreshed query-ids with
`python shared/auth/session_updater.py` (or edit `api_config` in `shared/config/config.json` directly).
The sniffer never writes config; `session_updater.py` owns the apply step.

> **Archive note:** the older response-only capture lives at the repo root in `graphql_sniffer.py` and
> `graphql_logs/` (26-file archive). It is **superseded** by the v4 Selenium sniffer and kept only as a
> historical reference — do not run it for new captures.

---

## Key Files Reference

### Entry Points (Run These)

| File | Purpose | Usage |
|------|---------|-------|
| `historical_scripts/historical_runner.py` | Fetches historical tweets for configured accounts | Run standalone |
| `live_scripts/live_runner.py` | Monitors live tweets, detects viral content | Run as continuous service |
| `search_scripts/search_runner.py` | Fetches search results via Advanced Search API | Run with `--once` or continuous mode |

### Shared Infrastructure

| File | Purpose | Key Classes/Functions |
|------|---------|----------------------|
| `shared/core/api_manager.py` | HTTP session, rate limiting, auth headers | `APIManager` |
| `shared/core/fetcher_engine.py` | Fetches pages, handles pagination, windowing | `FetcherEngine` |
| `shared/data_pipeline/storage_manager.py` | Raw page saving, processed tweet output, state management | `StorageManager` |
| `shared/core/set_operations.py` | Tweet set operations (intersection, union, diff) | `TweetSetProcessor` |
| `shared/core/windowing.py` | Rolling time window evaluation | `RollingWindowEvaluator` |

### Live Module (Isolated)

| File | Purpose | Key Classes |
|------|---------|-------------|
| `live_scripts/live_storage.py` | Live state management, viral snapshots | `LiveStorageManager` |
| `live_scripts/viral_detector.py` | Viral tweet detection logic | `ViralDetector` |

### Search Module (Isolated)

| File | Purpose | Key Classes |
|------|---------|-------------|
| `search_scripts/search_runner.py` | Search timeline monitoring, pagination, export | `SearchTimelineMonitor`, `SearchQueryBuilder` |

### Configuration

| File | Contents |
|------|----------|
| `shared/config/config.json` | API cookies, auth tokens, query IDs, feature flags |
| `shared/config/search_config.json` | Search queries, polling intervals, products |
| `shared/config/tier_config.py` | Account tiers, priority policies, pagination settings |

> **Warning:** `config.json` contains sensitive credentials (auth tokens, cookies). Do not commit to version control.

### Auth & Sniffer

| File | Purpose | Key Classes/Functions |
|------|---------|----------------------|
| `shared/auth/graphql_sniffer.py` | Headful Selenium capture of live GraphQL traffic + JS-generated auth headers; emits `timeline.jsonl`, `timeline.html`, `contract.json`, `playbook.md` | `observe(target, timeout_seconds, output_dir)`, `_extract_contract`, `_write_playbook` |
| `shared/auth/session_updater.py` | Applies captured query-ids into `config.json` (atomic save w/ backup); Playwright-based | `SessionUpdater`, `_apply_extracted`, `ENDPOINT_KEY_MAP` |
| `shared/auth/setup_api_cookies.py` | Interactive cookie harvest → `api_cookies` in `config.json` | — |

> The sniffer is **read-only** (no config writes). `session_updater.py` owns the write/apply step.

---

## Running the Project

### Prerequisites
- Python 3.11+
- `pytz` installed (`pip3 install pytz`)
- Valid API cookies (configure via `shared/auth/setup_api_cookies.py`)

### Running Each Component

```bash
# Historical fetcher
python historical_scripts/historical_runner.py

# Live monitor (continuous)
python live_scripts/live_runner.py

# Search monitor (one shot)
python search_scripts/search_runner.py --once

# Search monitor (continuous)
python search_scripts/search_runner.py --check-interval 60

# Search monitor (specific queries only)
python search_scripts/search_runner.py --once --only "My Search Name"

# GraphQL sniffer — capture live request shape (read-only; needs selenium + chromedriver)
python -m shared.auth.graphql_sniffer elonmusk --timeout 120
```

---

## Full Directory Tree

```
.
├── historical_scripts/
│   └── historical_runner.py
├── live_scripts/
│   ├── live_runner.py
│   ├── live_storage.py
│   └── viral_detector.py
├── search_scripts/
│   └── search_runner.py
├── shared/
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── graphql_sniffer.py   # headful Selenium capture → contract/playbook
│   │   ├── session_updater.py    # applies captured query-ids to config.json
│   │   └── setup_api_cookies.py
│   ├── config/
│   │   ├── __init__.py
│   │   ├── config.json
│   │   ├── search_config.json
│   │   └── tier_config.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── api_manager.py
│   │   ├── fetcher_engine.py
│   │   ├── set_operations.py
│   │   └── windowing.py
│   ├── data_pipeline/
│   │   ├── __init__.py
│   │   └── storage_manager.py
│   ├── exporters/
│   │   ├── __init__.py
│   │   └── text_export_helper.py
│   └── tools/
│       ├── check_replies_parity.py
│       └── diagnose_replies_only.py
├── data/                     # Generated data (not committed; see Data Storage Layout)
├── sniffer_runs/             # GraphQL sniffer captures (not committed — contains live creds)
├── structure.txt             # Auto-generated runtime tree snapshot (data/ + source modules)
└── repomix-output.md         # One-off packed repo export (reference only)
```

---

## State Management Matrix

| State File | Managed By | Location | Notes |
|------------|------------|----------|-------|
| `sync_state.json` | Historical/Live only (`StorageManager`, `manage_sync_state=True`) | `data/historical_live/state/` | Tracks endpoint cursors per account |
| `search_state.json` | Search only (`SearchTimelineMonitor`) | `data/search/state/` | Tracks last check time and tweet counts per search |
| `live_state.json` | Live only (`LiveStorageManager`) | `data/historical_live/state/` | Per-account polling state |
| `seen_tweets.json` | Live only (`LiveStorageManager`) | `data/historical_live/state/` | Tweet deduplication set |
| `snapshot_index.json` | Live only (`LiveStorageManager`) | `data/historical_live/state/` | Index of viral snapshots |

---

## Recent Refactoring (June 2026)

The search subsystem was refactored to fix three architectural flaws:

1. **Search Storage Isolation:** Search now uses `save_search_result_page()` instead of `save_raw_page()`, saving to `data/search/raw/...` only.
2. **State Management Isolation:** `StorageManager` now accepts `manage_sync_state=False` to prevent `sync_state.json` access by search.
3. **Folder Creation Isolation:** `StorageManager` now accepts `create_folders=False` to prevent the 5 standard user-data folders from being created by search.

### Modified Files

- `shared/data_pipeline/storage_manager.py` — Added `manage_sync_state`, `create_folders` parameters; added `save_search_result_page()` method
- `search_scripts/search_runner.py` — Updated `StorageManager` instantiation; replaced `save_raw_page` with `save_search_result_page`
