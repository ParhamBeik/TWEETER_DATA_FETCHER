# 07 - Live Monitoring And Viral Detection

Live monitoring and viral detection are preserved from the hybrid architecture. They should remain separate from transport experimentation.

## Live Monitor Purpose

`monitor_live_tweets_hybrid.py` is for incremental monitoring, not historical backfill.

It:

- polls configured accounts
- uses account tiers to decide frequency
- fetches a small number of pages
- filters to the live time window
- separates new and existing tweets
- saves snapshots
- runs viral detection

It should not deep crawl and should not use a different transport stack than historical mode.

## Account Tiers

Accounts are assigned tiers in code:

- Tier 1: highest priority, shortest interval.
- Tier 2: medium priority.
- Tier 3: lower priority, longest interval.

Tiering protects API budget and keeps important accounts fresher.

## Live Window

Live mode filters fetched items to the live window, currently expressed in hours. The monitor may fetch timeline pages that contain older items, but only recent items are processed for live output and viral snapshots.

This is different from historical mode, which is allowed to crawl deeper.

## Shared Fetching Path

The live monitor instantiates the historical fetcher and shares:

- `APIManager`
- `StorageManager`
- tweet parsing logic
- replies parsing logic
- conversation parsing logic

This is intentional. It prevents live mode from drifting away from historical mode.

## Snapshot Lifecycle

For each observed tweet, live mode saves engagement snapshots through `StorageManager`.

A useful viral signal requires at least two snapshots. More snapshots improve velocity, acceleration, and momentum calculations.

Snapshot fields include:

- likes
- retweets
- replies
- views
- quotes
- bookmarks

## Viral Scoring

`viral_detector.py` reads snapshot histories and calculates:

- engagement velocity per minute
- multi-window velocity
- acceleration
- engagement quality
- momentum
- account baseline comparison
- composite score

The classifier combines historical context and recent deltas. This is why the historical baseline should be collected before trusting live viral reports.

## Viral Reports

When a tweet crosses configured thresholds, storage writes a human-readable report under:

- `data/VIRAL/candidates/`
- `data/VIRAL/confirmed/`

Reports should include current metrics and growth context. Do not remove the snapshot basis from reports; it is the explanation for the classification.

## Rate-Limit Interaction

Live monitoring must respect endpoint budget:

- Keep page counts low.
- Use tier intervals.
- Preserve rate-limit state.
- Avoid retry storms on replies failure.

Replies transport fixes should not be implemented by increasing live polling pressure.

## Transport Rule For Live Mode

Live mode must preserve the same first replies behavior as historical mode:

- same session continuity
- same cookies
- same query IDs
- same warmup
- same referer and active-user ordering
- same no-cursor first request

If live and historical disagree, debug transport parity before changing viral logic.
