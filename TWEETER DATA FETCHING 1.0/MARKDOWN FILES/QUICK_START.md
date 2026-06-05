# Quick Start Guide - Twitter Fetcher

## 1. Setup

```bash
cd "TWEETER DATA FETCHING"
python3 setup_api_cookies.py
```

Paste cookies, bearer token, and query IDs when needed. For the integrated viral
workflow, `TweetDetail` is required for efficient per-tweet stat refreshes. If it
is missing, live monitoring still saves tweets, but viral detail refreshes are
skipped until `api_config.tweet_detail_query_id` is filled in `config.json`.

## 2. Fetch Historical Baseline

```bash
python3 fetch_historical_tweets.py
```

This fills `TWEETS/` with recent account history. The live monitor uses this
folder to calculate account-specific engagement baselines.

## 3. Run Live Monitoring and Viral Detection

```bash
python3 monitor_live_tweets.py
```

This is now the only live process. It:

- polls configured accounts continuously;
- saves normal live results into `TWEETS/{ACCOUNT}/{Jalali date}.txt`;
- writes live-added candidate batches into interval folders under `VIRAL TWEETS/`;
- uses `TweetDetail` on selected recent candidates to calculate delta-heavy viral scores.

## Output Folders

Regular tweets:

```text
TWEETS/
├── ELONMUSK/1405-02-16.txt
├── PAULG/1405-02-16.txt
└── WHALE_ALERT/1405-02-16.txt
```

Live candidate and viral snapshots:

```text
VIRAL TWEETS/
├── 5 Minutes/
│   ├── ELONMUSK/1405-02-19 04:47:56.txt
│   └── ELONMUSK/1405-02-19 04:52:56 VIRAL.txt
├── 30 Minutes/
├── 120 Minutes/
└── 600 Minutes/
```

## Configuration

Edit `config.json`:

```json
"viral_config": {
  "window_days": 7,
  "threshold_percentile": 95,
  "recheck_hours": 24,
  "intervals_minutes": [5, 30, 120, 600],
  "api_budget_mode": "balanced",
  "history_score_weight": 0.3,
  "delta_score_weight": 0.7
}
```

Important behavior:

- candidates must have been added by `monitor_live_tweets.py`;
- candidates outside the rolling `window_days` are ignored and cleaned up;
- historical files in `TWEETS/` are used for baselines, not as unlimited viral candidates;
- the monitor adapts live polling when viral refreshes consume API headroom.

## Troubleshooting

- `401 Unauthorized`: run `python3 setup_api_cookies.py` and update cookies.
- `404 Not Found`: update the relevant query ID in `config.json`.
- `TweetDetail query ID missing`: paste a `TweetDetail` GraphQL URL into setup or manually set `api_config.tweet_detail_query_id`.
- Rate limited: leave the monitor running; it reads rate-limit headers and stretches live polling when headroom is low.
