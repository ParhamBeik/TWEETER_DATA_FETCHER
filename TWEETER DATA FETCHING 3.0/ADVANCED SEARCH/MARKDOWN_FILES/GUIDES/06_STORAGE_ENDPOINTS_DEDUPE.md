# 06 - Storage, Endpoint Comparison, And Dedupe

The hybrid storage layer is one of the parts that should be preserved. Transport was restored from the old system, but storage remains centralized and endpoint-aware.

## Storage Responsibilities

`storage_manager.py` owns:

- Directory creation.
- Endpoint-separated output files.
- Merged timeline output.
- Endpoint diff output.
- Global dedupe registry.
- Engagement snapshot persistence.
- Viral report formatting.
- Operational log writing.

It should not make network requests and should not know how headers, cookies, query IDs, or warmup work.

## Endpoint Separation

The system intentionally keeps `UserTweets` and `UserTweetsAndReplies` outputs separate before merging.

Why this matters:

- `UserTweets` can remain healthy while replies fails.
- Endpoint-specific failures should be visible.
- Diff output helps diagnose missing replies or missing originals.
- Merged output should not hide transport problems.

Do not collapse endpoint storage into a single timeline too early.

## Endpoint Comparison

Endpoint comparison produces:

- Items only in `UserTweets`.
- Items only in `UserTweetsAndReplies`.
- Merged unique items.

Each tweet should carry source endpoint information when relevant. This makes it possible to answer:

- Did the tweet only appear in the main timeline?
- Did it only appear through the replies endpoint?
- Did both endpoints agree?
- Did replies fail entirely?

## Dedupe Registry

The global dedupe registry lives in:

`data/STATE/seen_tweets.json`

It records tweet IDs and where they were stored. This prevents repeated output churn across runs and lets live monitoring separate new tweets from existing tweets.

Rules:

- Dedupe by stable tweet ID.
- Do not dedupe by text.
- Do not dedupe by URL alone.
- Preserve the account and storage locations.
- Do not delete the registry unless intentionally rebuilding system state.

## Snapshots

Snapshots live in:

`data/SNAPSHOTS/{tweet_id}.json`

Each snapshot records metrics over time. Snapshot saving is filtered to avoid excessive writes:

- first snapshot is saved
- later snapshots are saved when enough time has passed or metrics changed enough
- thresholds are configured under `viral_detection.snapshot_delta_threshold`

Viral detection depends on snapshots. Storage changes that break snapshots will break viral detection.

## Logs

Operational logs live in `logs/`:

- `fetch_failures.log`
- `endpoint_health.log`
- `rate_limit.log`
- `viral_events.log`

Use logs to diagnose runtime behavior before changing code.

## Output Formatting

Formatting should preserve:

- tweet type
- author
- timestamp
- text
- metrics
- URL
- entity links/media/hashtags/mentions
- reply metadata
- conversation chain
- retweet original details
- quote original details
- endpoint source

Readable formatting is part of the product. Do not reduce outputs to raw JSON unless adding raw JSON as an additional artifact.

## Boundary Rule

Networking modules may pass parsed tweet dictionaries to storage. Storage may format and persist them. Storage must not decide request sequencing, query IDs, warmup behavior, or retry contexts.
