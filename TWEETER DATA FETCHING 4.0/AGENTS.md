# TWEETER DATA FETCHING 4.0 — Project Index

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

> **Note:** `LiveStorageManager` manages its own state files (`live_state.json`, `seen_tweets.json`, `snapshot_index.json`) independently from `StorageManager`.

### Search Module (Isolated)

| File | Purpose | Key Classes |
|------|---------|-------------|
| `search_scripts/search_runner.py` | Search timeline monitoring, pagination, export | `SearchTimelineMonitor`, `SearchQueryBuilder` |

> **Note:** Search uses `StorageManager` with `create_folders=False` and `manage_sync_state=False` to avoid side effects.

## Configuration Files

| File | Contents |
|------|----------|
| `shared/config/config.json` | API cookies, auth tokens, query IDs, feature flags |
| `shared/config/search_config.json` | Search queries, polling intervals, products |
| `shared/config/tier_config.py` | Account tiers, priority policies, pagination settings |

> **Warning:** `config.json` contains sensitive credentials (auth tokens, cookies). Do not commit to version control.

## State Management

| State File | Managed By | Location |
|------------|------------|----------|
| `sync_state.json` | Historical/Live only (`StorageManager` with `manage_sync_state=True`) | `data/historical_live/state/` |
| `search_state.json` | Search only | `data/search/state/` |
| `live_state.json` | Live only (`LiveStorageManager`) | `data/historical_live/state/` |
| `seen_tweets.json` | Live only (`LiveStorageManager`) | `data/historical_live/state/` |

## Recent Refactoring (June 2026)

The search subsystem was refactored to fix three architectural flaws:

1. **Search Storage Isolation:** Search now uses `save_search_result_page()` instead of `save_raw_page()`, saving to `data/search/raw/...` only.
2. **State Management Isolation:** `StorageManager` now accepts `manage_sync_state=False` to prevent `sync_state.json` access by search.
3. **Folder Creation Isolation:** `StorageManager` now accepts `create_folders=False` to prevent the 5 standard user-data folders from being created by search.

### Modified Files (Refactoring)

- `shared/data_pipeline/storage_manager.py` — Added `manage_sync_state`, `create_folders` parameters; added `save_search_result_page()` method
- `search_scripts/search_runner.py` — Updated `StorageManager` instantiation; replaced `save_raw_page` with `save_search_result_page`

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
```

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

## Key Architectural Decisions

1. **Three isolated subsystems:** Historical, Live, and Search each have their own state and storage.
2. **Shared infrastructure:** `StorageManager`, `APIManager`, `FetcherEngine` are shared but configurable via parameters.
3. **Search isolation:** Search subsystem uses `StorageManager` with `manage_sync_state=False` and `create_folders=False` to avoid polluting historical/live state and directories.
4. **Live isolation:** `LiveStorageManager` encapsulates all live-specific state (`live_state.json`, `seen_tweets.json`, snapshots) independently.
