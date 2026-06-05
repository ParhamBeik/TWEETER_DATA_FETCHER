# 03 - Output Format Standard

The hybrid system keeps the newer output architecture even though transport behavior was restored from the old experimental system. Output stability matters because humans read these files and downstream analysis depends on consistent structure.

## Output Directory Contract

`data/USER_TWEETS/`

Endpoint-separated output from `UserTweets`. This is the most stable timeline endpoint and should usually exist even when replies fail.

`data/USER_TWEETS_AND_REPLIES/`

Endpoint-separated output from `UserTweetsAndReplies`. This contains original tweets plus replies and conversation entries exposed by the replies timeline.

`data/MERGED_TIMELINES/`

Merged timeline built from both endpoint outputs. It should only be written when replies retrieval is healthy enough to produce meaningful comparison data.

`data/ENDPOINT_DIFFS/`

Comparison output that shows what each endpoint returned uniquely. This is diagnostic as well as analytical.

`data/SNAPSHOTS/`

JSON snapshot history by tweet ID. Viral detection reads these files.

`data/VIRAL/candidates/` and `data/VIRAL/confirmed/`

Human-readable viral reports emitted by `storage_manager.py`.

`data/STATE/`

Runtime state such as dedupe registry, rate-limit state, and endpoint health. These files affect future runs.

`logs/`

Operational logs for endpoint health, failures, rate limits, and viral events.

## Tweet Record Types

The parser classifies records into:

- `tweet`: original timeline tweet.
- `reply`: tweet whose `conversation_id` differs from its tweet ID or has reply metadata.
- `retweet`: wrapper around a retweeted original tweet.
- `quote`: tweet with quoted tweet metadata.

The formatter must make these visually distinct because the same endpoint can return several interaction types in one timeline.

## Required Fields

Each parsed tweet should preserve:

- `id`
- `username`
- `author_name`
- `timestamp`
- `text`
- `type`
- `url`
- `conversation_id`
- `in_reply_to_status_id`
- `in_reply_to_screen_name`
- `metrics`
- `entities`
- `source_endpoint`

Retweets should additionally preserve:

- `retweeted_tweet_id`
- `retweeted_author`
- `retweeted_text`
- `retweeted_timestamp`

Quotes should additionally preserve:

- `quoted_tweet_id`
- `quoted_author`
- `quoted_text`
- `quoted_timestamp`

Replies may additionally preserve:

- `conversation_chain`

## Metrics Contract

Engagement metrics should keep the richest values available:

- replies
- retweets
- likes
- quotes
- bookmarks
- views

Do not convert missing metrics to `0` unless the source explicitly means zero. Unknown values should remain distinguishable from true zero because viral scoring and human review interpret them differently.

## Conversation Readability

Conversation chains, replies, quoted tweets, and retweets must be visually separated in text output.

Rules:

- A reply should show its own text separately from the parent/conversation context.
- Conversation ancestry should be displayed as context, not blended into the reply body.
- A quote should show the quoting tweet separately from the quoted tweet.
- A retweet should show the account action separately from the original tweet.
- Endpoint source should remain visible in diff outputs.

The goal is not decoration. The goal is fast human interpretation without losing extraction fidelity.

## Output Compatibility

Do not casually rename directories, change file nesting, or remove fields from formatted output. Storage is a stable interface for:

- manual review
- endpoint debugging
- dedupe auditing
- viral reports
- future scripts

If the format must change, update these markdown files and document migration risk before changing code.
