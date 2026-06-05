# 04 - Troubleshooting

Start every transport investigation by separating first-request failures from pagination degradation. Latest runtime evidence confirms that this distinction is not cosmetic; it is the difference between request lifecycle failure and normal cursor invalidation.

## Highest Priority Question

Did the first `UserTweetsAndReplies` request return data?

If no, this is a request lifecycle failure. Check query ID freshness, session continuity, cookies, warmup, referer, feature payload, and cursor initialization.

If yes, and a later cursor page returns 404, this may be normal stale-cursor pagination behavior. Browser scrolling creates cursor-driven `UserTweetsAndReplies` requests, but those cursors are not durable state.

Do not treat those two cases the same.

## `UserTweets` Works But Replies Fail Immediately

Likely causes:

- `api_config.user_tweets_and_replies_query_id` is stale or from a different frontend deployment.
- Browser cookies are expired, incomplete, or filtered.
- `api_headers.x-client-transaction-id` is missing or not from the same real browser session.
- Warmup did not hit `https://x.com/{username}/with_replies`.
- The first replies request used the wrong referer.
- `x-twitter-active-user` context order changed.
- The first request accidentally included a cursor.
- Feature payload or `fieldToggles` diverged from the old working payload.
- The session was refreshed or recreated between warmup and the API call.

Correct response:

1. Refresh cookies, bearer token, transaction ID, and query IDs from a real browser session.
2. Verify warmup order.
3. Verify first replies request has no cursor.
4. Compare full request behavior with the old experimental fetcher.
5. Compare against a live browser capture if available.
6. Avoid adding new random contexts before restoring parity.

Do not conclude that the hybrid architecture is broken just because this request fails. Runtime evidence shows the architecture can work when these lifecycle conditions are correct.

## Replies Succeed Then Cursor Page 404s

This is a different condition.

Likely meaning:

- X/Twitter accepted the replies endpoint.
- The bottom cursor became stale or invalid.
- The endpoint does not want to paginate further for that request/session.
- The script reached the same class of behavior observed in real browser scrolling.

Correct response:

- Stop pagination gracefully.
- Keep the replies already retrieved.
- Do not mark the endpoint globally broken.
- Do not rewrite transport.
- Do not reuse the failed cursor in a later independent run.

## 401 Or Authentication Errors

Likely causes:

- Expired `auth_token`.
- Expired or mismatched `ct0`.
- Missing CSRF/header alignment.
- Browser session logged out.

Correct response:

```bash
python3 setup_api_cookies.py
```

Paste fresh cookies from the same authenticated browser session used to capture query IDs.

## 429 Or Rate-Limit Problems

Rate limits are tracked in `data/STATE/rate_limits.json` and updated from response headers.

Correct response:

- Wait until the reset time.
- Reduce account count or pages.
- Keep live monitoring page counts low.
- Do not bypass rate-limit accounting by deleting state unless you understand the current API budget risk.

## No Tweets Saved

Check:

- Account list in the active script.
- `logs/fetch_failures.log`.
- `logs/endpoint_health.log`.
- Whether `UserByScreenName` resolved a user ID.
- Whether the fetched tweets are outside the configured historical or live window.

For active scripts, use:

- `fetch_historical_tweets_hybrid.py`
- `monitor_live_tweets_hybrid.py`

Do not debug against stale `*_v2.py` assumptions unless intentionally running those files.

## No Merged Timeline Or Diff Output

Merged and diff outputs depend on both endpoint result sets being meaningful.

If `UserTweetsAndReplies` is unavailable for an account, the system may write only `USER_TWEETS` output and skip replies/merged/diffs for that run. This is intentional. It prevents fake mirrored data from hiding a replies transport failure.

## No Viral Reports

Check:

- At least two snapshots exist for a tweet in `data/SNAPSHOTS/`.
- Historical baseline exists in `data/USER_TWEETS/`.
- Snapshot delta thresholds are not too high for the account.
- The live monitor has run long enough to observe metric deltas.
- `logs/viral_events.log` has no save/classification errors.

Viral detection is snapshot-based. One observation is not enough.

## Debugging Method

For replies regressions, compare the first request against the old working runtime:

- URL and query ID.
- Variables JSON.
- Features JSON.
- `fieldToggles`.
- Full cookies attached to the session.
- Full headers.
- Referer.
- `x-twitter-active-user`.
- `x-client-transaction-id`.
- Warmup URL.
- Warmup delay.
- Session identity across `UserByScreenName`, `UserTweets`, warmup, and replies.
- Request ordering.

If a change cannot explain itself at the wire-behavior level, do not assume it is safe.

## Evidence Labels

When writing notes or changing docs, label findings clearly:

- Confirmed: observed in successful runtime or real browser capture.
- Experimental: seen during debugging but not repeatedly proven.
- Assumption: plausible explanation awaiting proof.
- Unresolved: known weakness that still needs monitoring.

This prevents future agents from turning guesses into architecture.
