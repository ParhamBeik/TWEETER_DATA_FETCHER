# 05 - Fetching Pipeline

This file describes how tweets are fetched and parsed in the active hybrid system. The storage and analytics layers are hybrid, and fetching behavior must remain aligned with the real X/Twitter frontend lifecycle.

## Historical Account Flow

For each account, `fetch_historical_tweets_hybrid.py` follows this shape:

1. Resolve user ID with `UserByScreenName`.
2. Fetch timeline pages from `UserTweets`.
3. Fetch timeline pages from `UserTweetsAndReplies`.
4. Parse tweets, replies, retweets, quotes, and conversation entries.
5. Attach conversation context.
6. Compare endpoint result sets.
7. Save endpoint-separated, merged, and diff outputs.
8. Register unique tweet IDs in the dedupe registry.
9. Save engagement snapshots for viral detection.

The order matters. Latest runtime evidence confirms that the replies endpoint should be reached through a stable session path, not as an isolated standalone request. The hybrid architecture can succeed when this behavior is preserved.

## GraphQL Request Shape

Timeline calls use:

- URL: `https://x.com/i/api/graphql/{query_id}/{operation_name}`
- `variables`: compact JSON string.
- `features`: compact JSON string.
- `fieldToggles`: compact JSON string.

For timeline endpoints, variables include:

- `userId`
- `count`
- `includePromotedContent`
- `withCommunity`
- `withVoice`
- `cursor` only after a bottom cursor has been received

The first page must not include a cursor.

`fieldToggles` should include:

```json
{"withArticlePlainText": false}
```

## Feature Payloads

The feature payload is intentionally broad and browser-like. Do not simplify it just because parsing only reads a subset of response fields.

X/Twitter treats GraphQL query ID, operation name, variables, features, field toggles, cookies, headers, and navigation context as a combined request contract. A smaller payload can be behaviorally different even if it seems semantically equivalent.

## `UserTweets` Flow

`UserTweets` is the more reliable timeline endpoint.

Request behavior:

- Uses the same session as account resolution.
- Uses `UserTweets` query ID from config.
- First page has no cursor.
- Later pages use `cursor-bottom-*` values.
- Referer is account/user-oriented.
- `x-twitter-active-user` is `yes`.

Parsing behavior:

- Reads timeline instructions.
- Handles `tweet-*` entries.
- Extracts bottom cursor.
- Stops when no cursor exists, page limit is reached, or timeframe boundary is reached.

## `UserTweetsAndReplies` Flow

`UserTweetsAndReplies` is more sensitive than `UserTweets`. It must follow frontend-like behavior, with the old working system and current successful runtime as reference points.

Before the first replies request:

1. Use the same session that resolved the user and fetched `UserTweets`.
2. Warm up `https://x.com/{username}/with_replies`.
3. Wait `api_config.replies_warmup_seconds`.
4. Send first GraphQL request with no cursor.

Context order:

1. Referer `https://x.com/{username}/with_replies`, active-user `no`.
2. Same referer, active-user `yes`.
3. Root referer `https://x.com/`, active-user `yes`.

Retries repeat this small context set. They should not create a large matrix of synthetic identities.

The first successful response is the health gate. Once the first response succeeds, later cursor failures should be analyzed as cursor lifecycle problems before transport problems.

## Pagination

Both timeline endpoints use bottom cursors:

- Find entries whose ID starts with `cursor-bottom-`.
- Store `content.value`.
- Use that value as `variables.cursor` on the next page.
- Stop when no bottom cursor exists.

Replies pagination has a special rule:

- 404 before first replies data means transport failure.
- 404 after replies data and with a cursor means stale cursor/pagination end first.

This distinction came directly from observing the old working system.

Latest browser evidence also confirms that scrolling the X/Twitter replies page produces additional cursor-driven `UserTweetsAndReplies` calls. Therefore cursor pagination is not a script invention. The fragile part is cursor validity, not the existence of cursor pagination.

Cursor lifecycle rules:

- Only use cursors returned by the immediately previous valid response.
- Do not persist cursors as config.
- Do not replay stale cursors across runs.
- Do not discard already retrieved replies when a later cursor fails.
- Do not mark first-page success as failure because pagination ended early.

## Timeline Entry Parsing

The parser handles two important entry families:

`tweet-*`

Direct tweet entries. These may be original tweets, retweets, quotes, or replies depending on legacy metadata and nested result objects.

`profile-conversation-*`

Conversation entries returned by the replies timeline. These can contain multiple tweets in a chain. The parser extracts each tweet and later attaches readable conversation context.

Do not discard conversation entries because they look harder to normalize. They are part of reply extraction reliability.

## Tweet Unwrapping

X/Twitter often wraps tweet objects under result layers. The parser unwraps common structures before extracting fields. This preserves compatibility across:

- normal tweet results
- tombstone-like wrappers
- nested retweeted status results
- quoted status results
- conversation entries

Parsing should be tolerant of missing fields and strict about preserving IDs.

## Text Extraction

Text extraction prefers richer text sources before falling back:

- note tweet text when present
- `legacy.full_text`
- `legacy.text`

Do not regress to a single text field. Long tweets, quote tweets, and nested originals often require the richer path.

## Retweets

A retweet is detected from:

- `legacy.retweeted_status_result`
- or text beginning with `RT @`

The parser preserves the action tweet and the original tweet metadata:

- original tweet ID
- original author
- original text
- original timestamp

The formatter should show the retweet action separately from the original tweet.

## Quote Tweets

A quote is detected from:

- `legacy.quoted_status_id_str`
- `quoted_status_result`

When the full quoted result is available, preserve:

- quoted tweet ID
- quoted author
- quoted text
- quoted timestamp

If only the quoted ID exists, keep the ID. Do not drop the relationship.

## Replies And Conversation Chains

A reply is identified by reply metadata and conversation identity:

- `conversation_id`
- `in_reply_to_status_id_str`
- `in_reply_to_screen_name`

Conversation chains are attached after parsing by grouping tweets from conversation entries. Output should show ancestry as context, not as part of the reply text.

## Live Fetching

`monitor_live_tweets_hybrid.py` reuses the historical fetcher for request and parsing behavior, but limits depth:

- Usually 1 to 2 pages.
- Filters to the configured live window.
- Processes new versus existing tweets.
- Saves snapshots for metric deltas.

Live mode must remain behaviorally compatible with historical mode.
