# 09 - UserTweetsAndReplies, Query IDs, And Cursor Lifecycle

This file is the dedicated engineering record for `UserTweetsAndReplies` failures, query IDs, cursor lifecycle, behavioral parity, transport/session continuity, stale cursor handling, and anti-bot observations.

Read this before changing replies fetching.

## Executive Summary

Latest successful runtime evidence confirms that the hybrid transport architecture was not fundamentally broken. The failures came from request behavior that did not match the real X/Twitter frontend closely enough.

The dominant gates are:

- fresh query IDs
- stable authenticated session continuity
- frontend-like first replies request lifecycle
- valid cursor progression
- graceful stale cursor handling

The key health signal is the first successful `UserTweetsAndReplies` response. If that succeeds, later cursor 404s are not automatically fatal.

## Confirmed Truths

These are established by old working behavior, browser observation, and latest successful runtime evidence:

1. `UserTweetsAndReplies` can work in the hybrid system.
2. The hybrid architecture itself was not the fundamental defect.
3. Query ID freshness is critical.
4. Query IDs are versioned frontend/backend contracts.
5. Session continuity is critical across user resolution, timeline fetches, warmup, and replies.
6. The first `UserTweetsAndReplies` request must be valid before pagination matters.
7. The first replies request should not include a cursor.
8. Browser scrolling creates additional cursor-driven `UserTweetsAndReplies` requests.
9. Cursor values have lifecycle/state validity and can become stale.
10. A 404 on a cursor page after successful replies retrieval is not necessarily endpoint failure.
11. Immediate 404 before any replies retrieval is a stronger signal of query/session/request mismatch.
12. Runtime browser-derived parameters matter more than synthetic fingerprint cleverness.

## Experimental Findings

These findings are strongly supported but should still be verified when X/Twitter changes:

- `/username/with_replies` warmup before the first replies request improves parity.
- The first context matching the old working flow is `/username/with_replies` referer with `x-twitter-active-user: no`.
- `x-twitter-active-user: yes` can be useful as a fallback, but changing it prematurely may diverge from the working path.
- Stable browser-derived `x-client-transaction-id` is safer than per-request synthetic generation in this project.
- Full browser cookie jars are safer than filtered minimal cookies.
- `UserTweets` can remain healthy while `UserTweetsAndReplies` fails.

## Current Assumptions

These are plausible explanations, not proven universal facts:

- X/Twitter may use 404 as a generic rejection response for invalid frontend/runtime context.
- Cursor validity may be tied to session, route, account, time, backend cache state, or scroll state.
- Some Cloudflare/browser-runtime values may affect acceptance even when they look temporary.
- The first replies request may be more sensitive because it establishes the server-side pagination context.

Do not encode these assumptions as elaborate fake-browser systems without browser evidence.

## Unresolved Bottlenecks

The system can still fail when:

- X/Twitter deploys new query IDs.
- Cookies expire or are copied from the wrong browser account/session.
- `ct0`, bearer token, and cookies become inconsistent.
- Cursor chains expire mid-run.
- The frontend changes feature flags, field toggles, or variables.
- Browser request captures are incomplete.

These are operational risks, not proof that parsing or storage is broken.

## Why Previous Architectures Failed

The failed architecture looked cleaner but diverged behaviorally:

- It treated headers as modular configuration rather than part of a live session lifecycle.
- It over-weighted synthetic anti-bot mutation.
- It treated transaction IDs as something to generate instead of browser-derived runtime state.
- It risked refreshing or mutating session state during sensitive replies retries.
- It treated 404s too uniformly.
- It did not prioritize the first successful replies request as the main health check.
- It made cursor-page failures look like endpoint failures.
- It allowed stale query IDs to masquerade as transport architecture problems.

The failure was not that modules existed. The failure was that abstractions changed wire behavior.

## Why The Newer Architecture Partially Succeeded

The newer architecture is useful where it does not alter frontend behavior:

- `APIManager` centralizes sessions, requests, query IDs, rate limits, and endpoint health.
- `StorageManager` preserves endpoint-separated outputs, merged timelines, diffs, snapshots, and dedupe.
- The historical fetcher keeps parsing, endpoint calling, pagination, and conversation extraction together.
- The live monitor reuses the historical fetch path instead of inventing another parser.
- Viral detection remains independent of transport.

The architecture succeeds when the transport layer behaves like the real frontend despite being modular internally.

## Why Runtime Browser-Derived Parameters Matter

X/Twitter GraphQL calls are not isolated JSON requests. A request is accepted or rejected in the context of:

- query ID
- operation name
- variables
- features
- field toggles
- cookies
- CSRF token
- bearer token
- transaction ID
- referer
- active-user state
- session history
- navigation route
- cursor state
- timing

Browser-derived values are valuable because they were produced by the same runtime X expects. Synthetic values may look valid but fail coherence checks.

## Stable Request Lifecycle

The stable lifecycle for replies is:

1. Create one `requests.Session`.
2. Load the full browser cookie jar into that session.
3. Set stable browser-derived headers.
4. Resolve the account with `UserByScreenName`.
5. Fetch `UserTweets` through the same session.
6. Warm up `https://x.com/{username}/with_replies` through the same session.
7. Wait the configured warmup delay.
8. Send first `UserTweetsAndReplies` request with:
   - fresh query ID
   - no cursor
   - old browser-like features
   - `fieldToggles`
   - `/with_replies` referer
   - stable session cookies
9. Parse returned timeline entries.
10. Extract bottom cursor if present.
11. Send cursor-driven requests in sequence.
12. Stop gracefully if no cursor exists or a cursor becomes stale.

Do not recreate the session between steps 1 and 8.

## Query ID Rules

Query IDs must be treated as deploy-versioned contracts.

Rules:

- Capture `UserTweets` and `UserTweetsAndReplies` query IDs from a real browser session.
- Refresh query IDs when first replies request starts failing.
- Keep query IDs aligned with the frontend version that produced the cookies when possible.
- Do not assume old query IDs are still valid.
- Do not assume a 404 means only stale query ID; compare the full lifecycle.

## Cursor Lifecycle Rules

Cursor rules:

- First request has no cursor.
- Later requests use bottom cursors from the immediately previous valid response.
- Do not invent cursors.
- Do not reuse old cursors across independent runs.
- Do not persist cursors as durable config.
- Treat cursor 404 after successful retrieval as likely stale cursor/pagination end.
- Treat first-request 404 as a transport/query/session problem.

Browser scrolling confirms that cursor-driven `UserTweetsAndReplies` requests are normal frontend behavior. It does not prove every cursor remains valid indefinitely.

## Stale Cursor Handling

Correct behavior after a cursor 404:

- Keep already retrieved replies.
- Stop pagination for that account/endpoint.
- Log the event as pagination degradation.
- Do not mark the entire endpoint stale if the first request succeeded.
- Do not retry the same cursor indefinitely.
- Do not refresh the whole session as a first response.

Incorrect behavior:

- Discard successful replies.
- Declare `UserTweetsAndReplies` globally broken.
- Replace query IDs without checking first-request success.
- Start generating synthetic headers.
- Re-run identical cursor requests repeatedly.

## Anti-Bot Observations

Confirmed:

- Coherent session continuity matters.
- Real browser-derived cookies matter.
- Query ID freshness matters.
- First replies request lifecycle matters.
- Over-randomization can hurt behavioral parity.

Not confirmed:

- That per-request synthetic transaction IDs improve reliability.
- That more route profiles improve reliability.
- That aggressive session refresh loops improve reliability.
- That a browser automation stack is required.

Design implication:

Keep anti-bot behavior modest and empirical: stable sessions, real cookies, realistic warmup, conservative timing, and no needless identity mutation.

## Debugging Checklist

When replies fail, answer in this order:

1. Did `UserByScreenName` succeed?
2. Did `UserTweets` succeed in the same session?
3. Did warmup hit `/username/with_replies`?
4. Did the first `UserTweetsAndReplies` request use a fresh query ID?
5. Did the first replies request have no cursor?
6. Did the first replies request return data?
7. If yes, did failure occur only after cursor pagination began?
8. If cursor failure occurred, was the failed cursor from the immediately previous response?
9. Are cookies, bearer token, CSRF token, and transaction ID from the same browser session?
10. Did any retry mutate the session or request identity?

Only after this checklist should code architecture be questioned.

## Future Development Rule

The correct target is not "old code forever" and not "clean abstraction at all costs." The correct target is:

Modular architecture with browser-faithful request behavior.

Any future change that affects `UserTweetsAndReplies` must prove it preserves the stable request lifecycle above.
