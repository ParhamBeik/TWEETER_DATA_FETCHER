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

Install dependencies:
```bash
pip3 install pytz
pip3 install jdatetime    # optional, improves Jalali formatting
pip3 install rich          # optional, improves terminal output
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
│   │   ├── session_updater.py
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
├── data/                     # Generated data (not committed)
├── logs/                     # Generated logs (not committed)
├── structure.txt             # Legacy project structure doc
└── repomix-output.md         # Packed repo output
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

