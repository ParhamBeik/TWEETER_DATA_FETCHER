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
| **Historical** | `historical_scripts/historical_runner.py` | Backfills a profile's tweets + replies (`UserTweets`, `UserTweetsAndReplies`). |
| **Live** | `live_scripts/live_runner.py` | Continuously polls timelines, dedups seen tweets, detects viral content. |
| **Search** | `search_scripts/search_runner.py` | Advanced Search (`SearchTimeline`) by keyword/phrase/account/product. |

All three build on the same shared core:

- `shared/core/api_manager.py` — authed session, rate-limit/backoff, the
  static + session headers, and a **fallback `x-client-transaction-id`**.
- `shared/core/fetcher_engine.py` — pagination + rolling time-windowing.
- `shared/data_pipeline/storage_manager.py` — raw page saving, the 5 processed
  tweet sets, state files, and subsystem routing.
- `shared/auth/` — cookie setup, query-id refresh, and the GraphQL sniffer.

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

`shared/auth/graphql_sniffer.py` is a **headful Selenium** tool that records the
real Twitter/X GraphQL traffic the browser sends — requests **and** responses —
including the per-request JavaScript-generated auth headers (`x-client-transaction-id`,
`query-id`) that a plain HTTP proxy can't see. It is **read-only**: it never
writes `config.json`.

```bash
# Capture a profile's timeline (headful Chrome opens; close it or wait for --timeout)
python -m shared.auth.graphql_sniffer elonmusk --timeout 120

# Point at any URL (search, with_replies, …)
python -m shared.auth.graphql_sniffer "https://x.com/search?q=Iran&f=live" --timeout 120
```

Each run emits four artifacts into `sniffer_runs/<jalali_batch>/`:

| Artifact | Purpose |
|---|---|
| `timeline.jsonl` | Arrival-ordered request/response events (URL, method, full headers, body, status). |
| `timeline.html` | Human waterfall; surfaces `x-client-transaction-id`, `x-csrf-token`, `x-twitter-auth-type`, rate-limit headers. |
| `contract.json` | Per-endpoint structured contract (query-id, url template, variables/features/fieldToggles, dynamic header notes, rate-limit sample). |
| `playbook.md` | Paste-ready markdown for AGENTS.md/README: endpoint table, static-vs-dynamic header split, `x-client-transaction-id` algorithm note. |

**On `x-client-transaction-id`:** this value is generated client-side by the
page JS per request (derived from method + path + a timing/animation seed) and
rotates every request; it is **not** a fixed pool and is **not** strictly
validated server-side. The project does **not** reimplement the generator —
`APIManager` synthesizes a stable fallback session tx-id so requests still carry
the header, which is sufficient for the endpoints in use. The sniffer is for
*understanding* the live shape, not replay. See AGENTS.md → "Sniffer-Derived
GraphQL Request Contract" for the full table + algorithm note.

> `sniffer_runs/` contains **live credentials** (auth tokens/cookies in headers)
> and is gitignored. `playbook.md` masks secret values; raw captures must not be
> shared. Requires Chrome + chromedriver (selenium-manager fetches the matching
> driver automatically; if blocked, `brew install --cask chromedriver`).

---

## Auth & query-id refresh

Twitter rotates GraphQL `query-id`s periodically. The fetchers read them from
`api_config` in `shared/config/config.json`.

```bash
# 1) Harvest fresh cookies (auth_token, ct0, …) into config.json
python shared/auth/setup_api_cookies.py

# 2) After a sniffer run, apply newly captured query-ids into config.json
python shared/auth/session_updater.py
```

`session_updater.py` owns the config **write** step (atomic save with backup);
the sniffer only observes. If cookies expire you'll see persistent 401/403 —
re-run `setup_api_cookies.py`.

> `config.json` holds secrets and is gitignored. `sniffer_runs/` likewise.

---

## Quickstart

```bash
cd "TWEETER DATA FETCHING 4.0"

# --- one-time setup ---------------------------------------------------------
pip3 install pytz jdatetime rich
pip3 install selenium playwright        # only for sniffer / session_updater
playwright install chromium             # only for session_updater

# Configure auth (creates shared/config/config.json)
python shared/auth/setup_api_cookies.py

# --- run a subsystem --------------------------------------------------------
python historical_scripts/historical_runner.py                 # backfill timelines
python live_scripts/live_runner.py                             # continuous live monitor
python search_scripts/search_runner.py --once                  # one search pass
python search_scripts/search_runner.py --once --only "My Search"

# --- capture the live request shape ----------------------------------------
python -m shared.auth.graphql_sniffer elonmusk --timeout 120
```

Common runner flags:

| Runner | Flags |
|--------|-------|
| historical | `--only <user>` (repeatable), `--no-user-tweets`, `--no-with-replies` |
| live | `--account <user>` (repeatable), `--once`, `--check-interval <s>` |
| search | `--only "<name>"` (repeatable), `--once`, `--check-interval <s>` |

Add accounts in `shared/config/tier_config.py`; define searches in
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
| `playwright` | `shared/auth/session_updater.py` only |

---

## Project layout (source map)

See [`structure.txt`](./structure.txt) for the full source-module map and
[`AGENTS.md`](./AGENTS.md) for architecture, the request contract, state
management, and troubleshooting.
