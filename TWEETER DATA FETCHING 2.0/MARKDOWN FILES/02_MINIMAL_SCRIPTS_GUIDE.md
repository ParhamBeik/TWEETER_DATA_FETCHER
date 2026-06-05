# 02 - Module Guide

This is the short map of the active hybrid runtime. For transport invariants, read `TRANSPORT_RULES_AND_BEHAVIORAL_INVARIANTS.md` first.

## User-Run Entry Points

`setup_api_cookies.py`

Bootstraps `config.json` from real browser-derived values. It should remain a capture helper, not a fingerprint generator. It collects cookies, bearer token, session-level `x-client-transaction-id`, and GraphQL query IDs.

`fetch_historical_tweets_hybrid.py`

Historical crawler. It owns endpoint calling order, timeline pagination, replies fetching, conversation extraction, tweet parsing, retweet parsing, quote parsing, and handoff to storage. It intentionally restores the old working fetch behavior while preserving the newer storage/analytics architecture.

`monitor_live_tweets_hybrid.py`

Incremental live monitor. It does not deep crawl. It polls account tiers, fetches recent pages, filters to the live window, saves snapshots, and invokes viral detection. It reuses the historical fetcher parsing path so live and historical output stay consistent.

## Core Modules

`api_manager.py`

Central networking module:

- Creates one stable `requests.Session`.
- Loads browser cookies into `.x.com`.
- Builds stable session headers.
- Applies small context overrides for referer and `x-twitter-active-user`.
- Performs GraphQL requests.
- Tracks rate limits from response headers.
- Tracks endpoint health.
- Runs the specific `/with_replies` warmup needed before first replies retrieval.

This module must not evolve into a synthetic browser-fingerprint engine.

`storage_manager.py`

Central storage and formatting module:

- Creates output directories.
- Maintains `data/STATE/seen_tweets.json`.
- Saves endpoint-separated timeline files.
- Compares endpoints into merged and diff outputs.
- Saves engagement snapshots.
- Formats tweets, retweets, quotes, replies, conversation chains, and viral reports.

Storage must remain separate from transport. Do not put request logic here.

`viral_detector.py`

Snapshot-based viral scoring:

- Loads snapshots from `data/SNAPSHOTS`.
- Calculates engagement velocity.
- Calculates multi-window velocity.
- Calculates acceleration and momentum.
- Compares current growth against account baselines.
- Classifies candidate or confirmed viral events.

It does not fetch from X/Twitter directly.

`config.json`

Durable runtime configuration. It should store real browser-derived session material and endpoint contracts, but not a growing pile of synthetic route profiles.

## Legacy Files In The Copy Project

`fetch_historical_tweets_v2.py` and `monitor_live_tweets_v2.py` may exist as older variants. The active hybrid entry points are:

- `fetch_historical_tweets_hybrid.py`
- `monitor_live_tweets_hybrid.py`

Do not update documentation or operational scripts to point at the older `*_v2.py` files unless the active architecture changes intentionally.

## Read-Only Behavioral Reference

The source-of-truth behavior for fetching lives outside this active copy:

`/Users/parham/Downloads/PERSONAL PROJECTS/EXPERIMENTS/TWEETER DATA FETCHING`

Reference files:

- `fetch_historical_tweets.py`
- `monitor_live_tweets.py`
- `setup_api_cookies.py`

Use them to answer "how did the working system behave?" Do not modify them while working on this copy.
