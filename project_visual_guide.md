# TWEETER DATA FETCHER — Visual Guide

This is the map view of the current root-level `src/` project.

## File Tree

```text
.
  README.md
  AGENTS.md
  project_visual_guide.md
  pyproject.toml
  requirements.txt
  src/
    pipelines/
      historical/fetch_historical.py
      live/monitor_live.py
      live/utils.py
      search/search_timeline.py
    shared/
      auth/auto_refresh.py
      auth/browser_context.py
      config/account_tiers.py
      config/config.example.json
      config/search_config.json
      core/pagination_engine.py
      core/tweet_processing_utils.py
      core/twitter_http_client.py
      data_pipeline/storage_manager.py
  diagnostics/
    probe_sequence.py
    probe_txid.py
    traffic_sniffer.py
    verify_contract.py
  tests/
    unit/
    integration/
    contract/
  tools/
    parity_check.py
```

Ignored local/generated paths:

```text
src/shared/config/config.json
data/
diagnostics/probe_runs/
diagnostics/sniffer_runs/
diagnostics/graphql_logs/
graphify-out/
```

## System Flow

```mermaid
flowchart TD
  CFG["account_tiers.py / search_config.json"] --> HIST["Historical"]
  CFG --> LIVE["Live"]
  CFG --> SEARCH["Search"]

  HIST --> FE["FetcherEngine"]
  LIVE --> FE
  SEARCH --> API["APIManager"]

  FE --> API
  FE --> RW["RollingWindowEvaluator"]
  FE --> STORE["StorageManager"]
  SEARCH --> STORE

  API --> AUTH["auto_refresh / browser_context"]
  API --> X["x.com GraphQL"]
  X --> FE
  X --> SEARCH

  FE --> SETS["TweetSetProcessor"]
  SEARCH --> SETS
  SETS --> STORE
  STORE --> DATA["data/"]
```

## Historical Pipeline

```mermaid
flowchart TD
  A["start historical"] --> B["resolve user ids"]
  B --> C["pass 1: UserTweets for every account"]
  C --> D["pass 2: UserTweetsAndReplies for every account"]
  D --> E["extract A and B"]
  E --> F["write 7 processed sets"]
  F --> G["write run report"]
```

Important stops:

- Stops pagination when the oldest seen tweet is at or before the absolute cutoff.
- Advances `fetch_watermark` only on completed endpoint fetches.
- Saves partial pages on cursor 404 or rate-limit exhaustion.

## Live Pipeline

```mermaid
flowchart TD
  A["start live cycle"] --> B["select due accounts"]
  B --> C["resolve user ids"]
  C --> D["pass 1: UserTweets"]
  D --> E["pass 2: UserTweetsAndReplies"]
  E --> F["write shared processed sets"]
  F --> G["seen_tweets dedup"]
  G --> H["viral snapshots/reports"]
```

Live no longer post-filters tweets by a separate UTC window. It uses the same cutoff stop as historical, with hour-level watermark flooring.

## Search Pipeline

```mermaid
flowchart TD
  A["start search"] --> B["load enabled search definitions"]
  B --> C["build rawQuery"]
  C --> D["fetch SearchTimeline pages"]
  D --> E["stop on rolling_hours or cursor end"]
  E --> F["write data/search raw/debug/processed"]
```

Search stays isolated under `data/search/`.

## Watermark Window

```mermaid
flowchart LR
  NOW["now"] --> BASE["now - configured window"]
  WM["fetch_watermark"] --> FLOOR["floor to day/hour"]
  BASE --> MIN["min(base, floored watermark)"]
  FLOOR --> MIN
  MIN --> STOP["pagination cutoff"]
```

Historical floor: day. Live floor: hour.

## Output Sets

```mermaid
flowchart TD
  A["UserTweets = A"] --> UNION["4_union"]
  B["UserTweetsAndReplies = B"] --> UNION
  A --> INTER["3_intersection"]
  B --> INTER
  A --> AMB["5_a_minus_b"]
  B --> BMA["6_b_minus_a"]
  AMB --> SYM["7_symmetric_difference"]
  BMA --> SYM
```

Folders:

- `1_user_tweets`: A
- `2_user_tweets_and_replies`: B
- `3_intersection`: A intersect B
- `4_union`: A union B
- `5_a_minus_b`: A minus B
- `6_b_minus_a`: B minus A
- `7_symmetric_difference`: A symmetric difference B

## Auth And Param Refresh

```mermaid
flowchart TD
  A["perform_get"] --> B{"status"}
  B -->|"200"| C["mark tx/qid healthy"]
  B -->|"404"| D["increment tx/qid failure count"]
  D --> E{"3 failures?"}
  E -->|"no"| F["try next candidate/retry policy"]
  E -->|"yes"| G["rule out candidate"]
  G --> H{"all candidates exhausted?"}
  H -->|"yes"| I["auto_refresh.py"]
  I --> J["capture fresh cookies/query IDs/tx IDs"]
  J --> K["rewrite local config.json"]
```

## Data State

| File | Purpose |
|---|---|
| `sync_state.json` | Historical/live endpoint cursor, status, watermark, raw batch path. |
| `live_state.json` | Live account poll status. |
| `seen_tweets.json` | Cross-run live tweet dedup. |
| `snapshot_index.json` | Viral snapshot lookup. |
| `tx_id_state.json` | Transaction ID health/failure count. |
| `query_id_state.json` | Query ID health/failure count. |
| `rate_limits.json` | Persisted endpoint reset/remaining counters. |
| `search_state.json` | Search query run state. |

## Verification Map

```text
tests/unit/test_unified_historical_live_plan.py
  rolling cutoff
  watermark flooring
  seven set math
  three-strike param state
  3600s rate cap

tests/integration/
  historical pipeline
  live pipeline
  search pipeline

tests/contract/
  sniffer and GraphQL contract expectations
```

Run:

```bash
.venv/bin/python -m pytest -q
python -m compileall -q src tests
graphify update .
```
