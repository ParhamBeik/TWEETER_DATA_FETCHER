# 01 - Setup And Run

This file explains how to bootstrap and operate the active hybrid system without breaking the transport behavior restored from the old experimental system.

## Project Directory

Run commands from:

```bash
cd "/Users/parham/Downloads/PERSONAL PROJECTS/TWEETER DATA FETCHING copy"
```

Do not run active development commands from the experimental source project. That project is the read-only behavioral reference.

## Dependencies

The runtime expects standard Python plus:

```bash
pip3 install requests jdatetime pytz
```

Do not install browser automation libraries to solve transport failures. The project intentionally avoids Selenium and Playwright for fetching.

## Configuration Bootstrap

Run:

```bash
python3 setup_api_cookies.py
```

The setup helper exists to capture durable browser-derived state from a real authenticated X/Twitter session:

- Browser cookies copied from an active session.
- Bearer token.
- `x-client-transaction-id` observed from the browser session.
- GraphQL query IDs for `UserByScreenName`, `UserTweets`, `UserTweetsAndReplies`, and optionally `TweetDetail`.

The setup helper should preserve the old system's philosophy: copy real browser-derived state instead of inventing synthetic state.

## Config Shape

`config.json` stores durable runtime configuration:

- `api_cookies`: full browser cookie jar used by `requests.Session`.
- `api_auth.bearer_token`: X/Twitter frontend bearer token.
- `api_headers.x-client-transaction-id`: browser-derived session-level transaction ID.
- `api_config.*_query_id`: versioned GraphQL operation IDs.
- `api_config.replies_warmup_seconds`: wait after `/with_replies` warmup.
- `api_config.replies_max_retries`: bounded retry rounds for replies contexts.
- `rate_limits`: local endpoint budget defaults.
- `anti_bot_simulation.delays_seconds`: modest human-like pacing values.
- `viral_config` and `viral_detection`: scoring and snapshot thresholds.
- `account_config`: account weighting options.

Do not remove browser cookies just because they look temporary. Do not remove `api_headers.x-client-transaction-id` just because it is not a permanent identity. In this project it is treated as browser-derived session state, not as a fake per-request fingerprint.

## Historical Baseline

Run:

```bash
python3 fetch_historical_tweets_hybrid.py
```

Historical mode:

- Resolves each account with `UserByScreenName`.
- Fetches `UserTweets`.
- Fetches `UserTweetsAndReplies` using the old working replies flow.
- Parses tweets, retweets, quotes, replies, and conversation chains.
- Writes endpoint-separated outputs.
- Writes merged and diff outputs when both endpoints are healthy.
- Registers tweet IDs in the centralized dedupe registry.
- Saves snapshots used by viral detection.

Build a historical baseline before relying on live viral classification, because viral scoring uses historical engagement context.

## Live Monitoring

Run:

```bash
python3 monitor_live_tweets_hybrid.py
```

Live mode:

- Uses the same `APIManager`, `StorageManager`, and historical fetcher parsing logic.
- Polls accounts according to tier.
- Limits collection to the live window instead of deep crawling.
- Saves engagement snapshots.
- Runs viral scoring after snapshots are available.

Live monitoring must not invent a different transport behavior. It shares the same session/cookie/header machinery as historical mode.

## Main Output Locations

- `data/USER_TWEETS/`: tweets returned by `UserTweets`.
- `data/USER_TWEETS_AND_REPLIES/`: items returned by `UserTweetsAndReplies`.
- `data/MERGED_TIMELINES/`: merged view across endpoint outputs.
- `data/ENDPOINT_DIFFS/`: endpoint comparison output.
- `data/SNAPSHOTS/`: engagement snapshots by tweet ID.
- `data/VIRAL/candidates/`: potential viral events.
- `data/VIRAL/confirmed/`: confirmed viral events.
- `data/STATE/`: dedupe, rate-limit, and endpoint health state.
- `logs/`: endpoint health, fetch failure, rate limit, and viral logs.

## Operating Rules

- Refresh cookies and query IDs from a real browser session when transport degrades.
- Treat a first-request `UserTweetsAndReplies` 404 as a transport parity problem.
- Treat a later cursor 404 after successful replies retrieval as likely pagination degradation.
- Prefer fewer, stable retries over broad random mutation.
- Do not clear state files casually. State is part of rate-limit accounting and dedupe history.
