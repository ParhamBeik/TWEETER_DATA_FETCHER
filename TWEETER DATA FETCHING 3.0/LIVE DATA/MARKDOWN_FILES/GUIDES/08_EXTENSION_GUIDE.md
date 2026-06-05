# 08 - Extension Guide

Use this checklist before extending the project. Most regressions in this codebase are likely to come from changing transport behavior while intending to improve architecture.

Latest runtime evidence refined this rule: modular architecture is allowed. The failure mode is not architecture itself; it is architecture that changes the request lifecycle X/Twitter expects.

## Safe Change Order

1. Read `TRANSPORT_RULES_AND_BEHAVIORAL_INVARIANTS.md`.
2. Read `09_USERTWEETSANDREPLIES_QUERY_IDS_AND_CURSOR_LIFECYCLE.md`.
3. Identify whether your change affects wire behavior.
4. If it affects transport, compare against the old experimental system and current successful runtime behavior.
5. If it affects output, update `03_OUTPUT_FORMAT_STANDARD.md`.
6. If it affects storage state, document migration implications.
7. Run small account tests before broad historical or live runs.

## Adding Or Updating Query IDs

Query IDs are X/Twitter frontend/backend contracts.

Rules:

- Capture query IDs from a real browser session.
- Prefer query IDs from the same browser/runtime session as the cookies.
- Keep `UserTweets` and `UserTweetsAndReplies` in sync with the same observed frontend deployment when possible.
- Do not treat operation names as enough.
- Do not blindly copy query IDs from old notes if the live browser has changed.

If replies first request starts returning 404, query ID parity is one of the first things to check.

Runtime evidence confirms stale or mismatched query IDs can masquerade as architecture failure. Refresh IDs before redesigning transport.

## Changing Headers

Header changes are transport changes.

Before changing headers, capture or inspect real browser behavior. Then document:

- old value
- new value
- endpoint affected
- browser evidence
- expected behavior change

Do not add synthetic browser headers just because they look plausible. Coherence matters more than completeness.

## Changing Cookies

Cookie changes are high risk.

Do not filter the cookie jar for neatness. The old system worked by copying the browser's authenticated session. Temporary-looking cookies can still affect Cloudflare or X/Twitter request validation.

If cookies expire, refresh from the browser. Do not compensate with random headers.

## Changing Replies Fetching

Any replies change must preserve:

- same session used before replies
- `/username/with_replies` warmup
- configured warmup sleep
- no cursor on first request
- first context with `/with_replies` referer and active-user `no`
- small retry context set
- stale cursor 404 tolerance after successful retrieval

If you cannot prove these are preserved, do not merge the change.

Additionally, prove that the first replies request succeeds before judging pagination quality. Pagination can degrade after success through cursor invalidation.

## Adding A New Endpoint

Keep endpoint logic isolated:

- Add query ID config.
- Add rate-limit config.
- Add endpoint health tracking.
- Add request variables/features based on browser evidence.
- Add parser support only for response shapes actually returned.
- Add storage output only if it has a clear role.

Do not let a new endpoint change `UserTweetsAndReplies` behavior.

## Changing Parsing

Parsing changes should preserve:

- note tweet text fallback
- `legacy.full_text` fallback
- retweeted status extraction
- quoted status extraction
- reply metadata
- conversation chain grouping
- entity extraction
- metric richness

Transport failures should not be solved in parsing code.

## Changing Storage

Storage changes should preserve:

- endpoint-separated outputs
- merged outputs
- endpoint diffs
- dedupe registry
- snapshots
- viral report paths

If changing a file format, document:

- old path/format
- new path/format
- migration behavior
- downstream risk

## Changing Viral Detection

Viral changes should preserve the snapshot model. Do not replace snapshot deltas with a single current metric observation.

Before changing thresholds, verify:

- enough snapshots exist
- historical baseline exists
- account size differences are considered
- missing metrics are not treated as zero by accident

## Verification Checklist

For transport-affecting changes:

- `UserByScreenName` resolves.
- `UserTweets` returns data.
- First `UserTweetsAndReplies` request returns data.
- Later cursor 404, if present, is handled gracefully.
- Endpoint health logs reflect first-request failures differently from cursor termination.
- Cookies and transaction ID remain browser-derived.
- The session is not recreated during replies retries.
- Cursor values are used only from live responses, not persisted or invented.
- Query IDs were refreshed from browser evidence when first-request failures appeared.

For storage/format changes:

- endpoint folders are written as expected
- merged and diff outputs remain readable
- dedupe registry updates
- snapshots are saved
- viral detector can load snapshots

## Forbidden Shortcuts

Do not:

- refactor transport for elegance before proving behavioral parity
- generate per-request fake transaction IDs
- randomize identity headers aggressively
- remove `/with_replies` warmup
- collapse endpoint outputs too early
- ignore first-request replies 404s
- treat cursor 404s as equivalent to first-request failures
- mistake query ID staleness for proof that the modular architecture is broken
- modify the experimental source project while working in the active copy
