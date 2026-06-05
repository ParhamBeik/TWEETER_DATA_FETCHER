# Twitter Fetcher Implementation Summary

## Overview

The viral detection workflow has been merged into `monitor_live_tweets.py`.
`detect_viral_tweets.py` has been removed, so live fetching, candidate snapshots,
TweetDetail stat refreshes, and viral scoring now run in one process.

## Current Workflow

1. `fetch_historical_tweets.py` builds the rolling historical baseline in `TWEETS/`.
2. `monitor_live_tweets.py` continuously polls configured accounts.
3. Newly added live tweets are saved to both:
   - regular daily files in `TWEETS/{ACCOUNT}/`;
   - timestamped candidate files in `VIRAL TWEETS/{Interval}/{ACCOUNT}/`.
4. When a configured viral interval is due, the monitor uses `TweetDetail` to
   refresh only selected recent live candidates.
5. Viral files are written next to the candidate snapshots with `VIRAL` in the
   filename.

## Key Configuration

`config.json` now includes:

```json
"api_config": {
  "tweet_detail_query_id": ""
},
"rate_limits": {
  "TweetDetail": {"limit": 150, "window_seconds": 900}
},
"viral_config": {
  "window_days": 7,
  "threshold_percentile": 95,
  "recheck_hours": 24,
  "intervals_minutes": [5, 30, 120, 600],
  "api_budget_mode": "balanced",
  "max_detail_refreshes_per_run": 30,
  "history_score_weight": 0.3,
  "delta_score_weight": 0.7,
  "composite_score_cutoff": 1.0,
  "delta_percentile_cutoff": 0.8
}
```

`TweetDetail` is intentionally optional at startup. If the query ID is blank,
the monitor still saves live tweets and candidate snapshots, but it skips viral
stat refreshes until the query ID is configured.

## Viral Scoring

- Historical baseline: calculated from `TWEETS/` over `window_days`.
- Live candidate pool: only tweets that were added by `monitor_live_tweets.py`
  and saved under `VIRAL TWEETS/`.
- Delta metric: weighted growth in likes, retweets, replies, quotes, bookmarks,
  and views, with engagement actions weighted above views.
- Composite score: `30%` historical engagement normalization and `70%` interval
  delta percentile.
- A candidate is viral when it passes both `composite_score_cutoff` and
  `delta_percentile_cutoff`.

## Output Structure

```text
TWEETER DATA FETCHING/
├── config.json
├── fetch_historical_tweets.py
├── monitor_live_tweets.py
├── setup_api_cookies.py
├── TWEETS/
└── VIRAL TWEETS/
    ├── 5 Minutes/
    │   └── ELONMUSK/
    │       ├── 1405-02-19 04:47:56.txt
    │       └── 1405-02-19 04:52:56 VIRAL.txt
    ├── 30 Minutes/
    ├── 120 Minutes/
    └── 600 Minutes/
```

The legacy `VIRAL_TWEETS/` folder is not used by the new workflow and is left
untouched if it already exists.

## Operational Notes

- Run only `monitor_live_tweets.py` for live monitoring and viral detection.
- The monitor reads X rate-limit headers and reduces live polling pressure when
  viral TweetDetail refreshes consume request headroom.
- Candidate files outside the configured rolling window are ignored and cleaned
  up by the monitor.
- Use `setup_api_cookies.py` to capture `TweetDetail` query IDs from pasted
  GraphQL URLs or HAR files.
