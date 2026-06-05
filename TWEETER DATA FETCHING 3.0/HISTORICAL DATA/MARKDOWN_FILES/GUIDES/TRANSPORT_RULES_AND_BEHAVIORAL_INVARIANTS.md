# Transport Rules And Behavioral Invariants

This is the transport constitution for the project. Future engineers and LLM agents must read this before changing fetching, sessions, headers, cookies, query IDs, replies, pagination, warmup, or retry behavior.

## Debugging History

The old experimental system was messy but reliable. The newer hybrid system was cleaner but regressed `UserTweetsAndReplies`. Parsing, formatting, storage, dedupe, endpoint comparison, and viral detection were not the main failure areas. The failure was request behavior.

Latest successful runtime evidence refined the diagnosis: the hybrid transport architecture itself was not fundamentally broken. The dominant bottleneck was whether the request lifecycle matched the real X frontend runtime closely enough. Query ID freshness, session continuity, cursor validity, and the first replies request lifecycle were decisive.

The hybrid refactor initially treated request construction as a clean abstraction problem. It added route-aware contexts, dynamic transaction IDs, session refreshes, and more sophisticated anti-bot behavior. That was the wrong direction when it changed observed frontend behavior. X/Twitter appears to care more about coherent browser-derived runtime continuity than synthetic variability.

The old system received 404s too, but the timing mattered:

- Old working behavior: `UserTweetsAndReplies` first succeeds, then later pagination can return 404 for stale cursors.
- Broken hybrid behavior: `UserTweetsAndReplies` fails before first valid replies retrieval.

These are different classes of failure. A pagination 404 after data retrieval is tolerable. A first-request 404 indicates transport, session, query ID, cookie, referer, or warmup mismatch.

Browser evidence also shows that scrolling a replies page produces additional cursor-driven `UserTweetsAndReplies` requests. Therefore cursor pagination is real frontend behavior, but cursor lifecycle validity is fragile.

## Evidence Categories

Confirmed truths:

- Fresh query IDs matter.
- Stable session continuity matters.
- The initial successful `UserTweetsAndReplies` request is the most important health signal.
- Cursor-driven pagination is real frontend behavior.
- Cursor 404s can happen during normal operation after successful retrieval.
- Pagination 404 is not automatically endpoint failure.
- The active hybrid system can succeed when request behavior matches the frontend closely enough.

Experimental findings:

- `UserTweetsAndReplies` is more sensitive than `UserTweets`.
- `/username/with_replies` warmup improves parity with real navigation.
- Referer and active-user combinations influence replies acceptance.
- Session-derived runtime values are safer than synthetic mutation.

Assumptions:

- X/Twitter validates a combined request/runtime state, not only individual headers.
- Some 404s are intentionally used as rejection responses for invalid request context.
- Cursor invalidation may depend on account, session, time, scroll state, or backend shard state.

Unresolved bottlenecks:

- Query IDs can expire at any time with frontend deployments.
- Browser-derived cookies and transaction IDs can expire or become inconsistent.
- Cursor chains can terminate unexpectedly.
- Complete byte-for-byte parity with browser runtime is not guaranteed without live capture.

## Permanent Rules

1. Behavioral parity matters more than architectural elegance.
2. X/Twitter validates request lifecycle coherence, not isolated headers.
3. Stable coherent sessions outperform synthetic anti-bot mutation.
4. Query IDs are frontend/backend contracts, not generic endpoint names.
5. Query ID freshness is a first-class dependency.
6. `UserTweetsAndReplies` is more sensitive than `UserTweets`.
7. Warmup navigation is part of the request lifecycle, not optional decoration.
8. Consistency beats fingerprint randomization.
9. The first successful replies request is the key health signal.
10. Pagination 404 after successful retrieval can be normal and should be tolerated.
11. Immediate first-request 404 means transport/session/query/cursor lifecycle mismatch.
12. Real browser behavior takes precedence over clean abstractions.
13. Cursor-driven pagination is real frontend behavior, but cursor validity is temporary.
14. The hybrid architecture is allowed to be modular as long as its wire behavior remains frontend-compatible.

## The Old Working First Replies Flow

The old system's first replies call follows this sequence:

1. Initialize one `requests.Session`.
2. Load all browser cookies from config into `.x.com`.
3. Set stable browser-derived headers once.
4. Resolve `UserByScreenName`.
5. Fetch `UserTweets` pages first.
6. Before first replies page, warm up:
   - `GET https://x.com/{username}/with_replies`
   - use the same session
   - use session default headers
   - wait `replies_warmup_seconds`
7. Send first `UserTweetsAndReplies` request with no cursor.
8. First context:
   - referer: `https://x.com/{username}/with_replies`
   - `x-twitter-active-user`: `no`
9. If that fails, try:
   - same referer, active-user `yes`
   - root referer, active-user `yes`
10. Retry rounds use the same session. They do not rebuild the session.

This exact shape matters.

## First Replies Request Invariants

For the initial `UserTweetsAndReplies` call:

- Variables must contain `userId`, `count`, `includePromotedContent`, `withCommunity`, `withVoice`.
- Cursor must be absent.
- Feature payload must match the old broad browser-like feature set.
- `fieldToggles` must include `{"withArticlePlainText": false}`.
- The session must be the same session used for prior `UserByScreenName` and `UserTweets`.
- The request headers should be cloned from session headers and then override only:
  - `referer`
  - `x-twitter-active-user`
- Do not synthesize a new transaction ID per request.
- Do not refresh the session before first replies.
- Do not add a new unrelated profile warmup before `get_user_id`.
- Do not pre-block the request because of stale persisted rate-limit state.

## Headers

The stable session header model comes from the old fetcher:

- `authorization`
- `x-csrf-token`
- `x-twitter-active-user`
- `x-twitter-auth-type`
- `x-twitter-client-language`
- `x-client-transaction-id`
- `user-agent`
- `referer`
- `accept`
- `content-type`
- `dnt`
- `priority`
- `sec-ch-ua`
- `sec-ch-ua-mobile`
- `sec-ch-ua-platform`
- `sec-fetch-dest`
- `sec-fetch-mode`
- `sec-fetch-site`

Do not replace this with a highly dynamic fingerprint generator. When a browser-derived `x-client-transaction-id` is available, use it at session level. If missing, one fallback session value may be generated, but that is a fallback, not an anti-bot strategy.

## Cookies

The old system copied browser cookies directly. The session coherence appears to depend on the whole browser-derived cookie jar, not only `auth_token` and `ct0`.

Do not casually filter browser cookies out of config for cleanliness. Some cookies may look temporary, but if the live browser sent them during a working session, removing them can change behavior.

If cookies expire, refresh them from the browser. Do not compensate by adding synthetic headers.

## Query IDs

Query IDs are versioned contracts generated by X/Twitter's frontend/backend deployment. They are not just endpoint labels.

Rules:

- Keep `UserTweets` and `UserTweetsAndReplies` query IDs from the same observed browser session when possible.
- Do not assume a 404 means only a stale query ID.
- For first-request replies 404, check query ID parity, cookie parity, warmup, referer, and session sequence.
- For cursor-page replies 404 after successful retrieval, treat it as pagination termination unless evidence says otherwise.

## Warmup

Warmup is part of replies transport:

- Warm only `/username/with_replies` before first replies request.
- Use the same session.
- Use default session headers.
- Sleep `replies_warmup_seconds`.
- Do not replace this with many synthetic navigation routes.
- Do not rebuild the session after warmup.

## Retry Ordering

Retries for replies must remain simple:

1. `/username/with_replies`, active-user `no`
2. `/username/with_replies`, active-user `yes`
3. `/`, active-user `yes`

Repeat that small set for the configured number of retry rounds. Do not expand this into many context profiles. More variation can make the session less coherent.

## Pagination Rules

The first replies request has no cursor. Later pages use `cursor-bottom-*` values from timeline entries.

Rules:

- Continue while a bottom cursor exists.
- Stop when no cursor exists.
- If `UserTweetsAndReplies` returns 404 while using a cursor after prior success, treat it as stale cursor / pagination end.
- Do not mark the whole endpoint broken because of a cursor 404.
- Do mark first-request 404 as a transport health problem.

## What Broke Replies

The hybrid regression came from behavioral drift, not from storage or parsing:

- Stale or mismatched query IDs.
- Missing browser-copied cookie values.
- Extra warmup/request sequencing before `get_user_id`.
- Method-level headers that did not exactly clone the old full session-header request shape.
- Pre-request rate-limit gating based on persisted hybrid state.
- Dynamic per-request transaction IDs and session refresh loops in earlier iterations.
- Too much focus on abstract request contexts and not enough on first-request parity.
- Treating all 404s as endpoint failure instead of separating first-request rejection from cursor invalidation.

## Why The Newer Architecture Partially Succeeded

The newer hybrid architecture partially succeeded because its modular boundaries were not inherently wrong:

- Centralized session management can work.
- Centralized rate-limit accounting can work.
- Endpoint health tracking can work.
- Endpoint-separated storage can work.
- Shared parsing between historical and live modes can work.

It failed only when those modules changed runtime behavior away from the frontend. The lesson is not "avoid architecture." The lesson is "architecture must preserve request behavior."

## Why The Fixes Worked

The restored behavior makes the first replies request look like the old runtime path:

- Same query ID family.
- Same session continuity.
- Same cookie philosophy.
- Same warmup URL and timing.
- Same referer/active-user ordering.
- Same feature payload.
- Same no-cursor first request.
- Same tolerance for later cursor 404s.
- Clear separation between first-request failure and cursor-page degradation.

## Forbidden Architectural Mistakes

Do not:

- Generate a fresh fake transaction ID for every request.
- Refresh/recreate the session during replies retries.
- Add many synthetic route profiles without browser evidence.
- Treat all 404s equally.
- Remove warmup because it looks redundant.
- Pre-block the first replies request using stale local rate-limit state.
- Simplify feature payloads just because the response parser does not use every field.
- Filter browser cookies because they look temporary.
- Refactor transport before proving byte-for-byte behavioral parity of the first replies request.

## Debugging Methodology

When replies break:

1. Ask: did the first `UserTweetsAndReplies` request return data?
2. If no, compare the first request to the old working path:
   - URL and query ID
   - variables JSON
   - features JSON
   - field toggles
   - cookies attached
   - full headers
   - referer
   - active-user
   - transaction ID
   - warmup URL
   - warmup timing
   - session identity
   - request sequence before replies
3. If first request succeeds and later cursor fails, treat as pagination degradation first.
4. Refresh query IDs and cookies from a real browser session before inventing transport behavior.
5. Only after behavior matches should you consider code organization.

## Design Philosophy

The transport layer is not a place to be clever. It is a place to be boring, stable, and empirically faithful.

The hybrid system may be modular internally, but its wire behavior must remain old-system compatible until a real browser capture proves a new behavior is required.
