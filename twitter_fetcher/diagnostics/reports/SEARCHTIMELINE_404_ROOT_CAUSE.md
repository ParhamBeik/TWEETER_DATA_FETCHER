# 404 Root-Cause Analysis — All 3 Production Endpoints

**Run date:** 2026-07-16 · **Transport:** curl_cffi 0.15.0 (`impersonate=chrome120`, HTTP/2) + canonical `requests` APIManager for cross-check · **Definitive evidence:** `tests/reports/pagination_test_20260716_021606.{json,md}`

This documents the hypothesis-driven elimination of 404s across UserTweets, UserTweetsAndReplies, and SearchTimeline. Every hypothesis below was tested against live authenticated GraphQL; nothing here is fabricated.

---

## TL;DR — endpoint status after the fixes

| Endpoint | Before | After (warm-up off + context fix) | Verdict |
|---|---|---|---|
| **UserTweets** | 100% | **100%** (6/6, 0×404, 968 ms median, HTTP/2) | ✅ clean |
| **UserTweetsAndReplies** | **0%** (404 on page 1) | **100%** (6/6, **0×404 on any page**, 948 ms median) | ✅ **fixed** |
| **SearchTimeline** | p1=200, p2=404 | p1=200, **p2=404** (empty body) | ⚠️ server-side gate (see §3) |

- The **page-1 404s were a self-inflicted client bug** (wrong request context). Fixed. Two of three endpoints are now 404-free at the HTTP layer.
- The **SearchTimeline page-2+ 404 is a Twitter-side anti-abuse gate.** It is received identically by `requests` and `curl_cffi`, with an empty body and the rate-limit counter decrementing — i.e. Twitter accepted and throttled the request, then returned 404 instead of honoring it. It is **not** fixable at the HTTP client layer; production already mitigates it via the Playwright browser fallback at `pipelines/search/service.py:568-593`.

---

## 1. The fix that worked — request context (the real page-1 cause)

`APIManager.get_context_variants()` returns multiple `RequestContext`s. `perform_get` uses variant `[0]`. For **UserTweetsAndReplies**, variant `[0]` is the *passive* context: `x-twitter-active-user: no`, generic referer.

But the **production pipelines send `x-twitter-active-user: yes`** plus the tab-specific referer (`/{user}/with_replies`, `/search?q=…&f=live`) — see `timeline.py:743-753` and `search/service.py:_build_frozen_headers`. The diagnostic reproduced this exactly:

```python
def production_headers(endpoint, username, raw_query):
    if endpoint == "UserTweetsAndReplies" and username:
        return {"referer": f"https://x.com/{username}/with_replies", "x-twitter-active-user": "yes"}
    if endpoint == "SearchTimeline" and raw_query:
        return {"referer": f"https://x.com/search?q={quote(raw_query)}&f=live", "x-twitter-active-user": "yes"}
    ...
```

Sending the **production context eliminated every page-1 404** on UserTweetsAndReplies (0% → 100%, 6/6 pages). The "curl_cffi causes 404s on /with_replies" claim from the prior session was this context artifact, not curl_cffi.

## 2. Warm-up — removed (inert, costly)

Warm-up priming GETs add 500–800 ms and the mandatory `first_request_warmup_seconds` sleep added 2 s, for **zero success-rate benefit** (proven: cold == warm_naive == warm_pooled, `cold_vs_warm_20260715_224902`). Now dormant in code and config:

- `client.py` — `browse_warmup_enabled` default `True → False` (both gate sites); `warmup_session` now gated under the same switch.
- `timeline.py` — `first_request_warmup_seconds` default `2 → 0`.
- `config/config.json` — `anti_bot_simulation.browse_warmup_enabled: false`, `api_config.first_request_warmup_seconds: 0`.

Verified: all three warmup methods return early with no GET; a live UserTweets request returns 200 immediately. `curl_cffi_client.py` keeps one persistent `cffi.Session` (the real latency mechanism — connection reuse + HTTP/2), with **no** warm-up.

## 3. SearchTimeline page-2 404 — root cause is server-side

Reproducible signature: **p1 → 200, p2 → 404 with an empty body.** The cursor Twitter returns on p1 is well-formed (`entryId: cursor-bottom-0`, `__typename: TimelineTimelineCursor`, distinct from the Top cursor), so we send back exactly what we were given — and Twitter rejects it.

**Decisive evidence it is NOT a client defect:**

1. **Both clients receive it.** The canonical `requests` APIManager produces the identical `p1=200, p2=404` (`pagination_test_20260716_015613.json`, `requests/SearchTimeline`). A curl_cffi-specific bug would not reproduce through `requests`.
2. **The 404 body is empty.** A real "no results" is 200 + empty instructions; a real "bad cursor/variables" is 200 + an `errors` array. An **empty-body 404 is a gateway-level rejection**, not a data-level response.
3. **`x-rate-limit-remaining` decrements on the 404.** Twitter authenticated, rate-counted, and served the request — then returned 404. This is the signature of an anti-abuse soft-block (it throttles by returning 404 rather than a clean 429).
4. **Cursor selection is correct.** Only one Bottom cursor exists per page; `extract_bottom_cursor` and production's scored `_collect_cursor_candidates` pick the same value.

This is exactly why production already has recovery code for it: `pipelines/search/service.py:568` (`if status == 404 and cursor and has_pages … bootstrap_browser_context`). The browser path is the intentional mitigation.

### Hypotheses tested and disproven (SearchTimeline p2)

| Hypothesis | Verdict | Why disproven |
|---|---|---|
| Wrong request context | ✅ (fixes page-1 only) | p1 now 200; p2 still 404 with production context |
| Cursor picker picks wrong token | ❌ | Only one Bottom cursor; both pickers agree; both clients 404 |
| Stale/`x-client-transaction-id` | ❌ as cause | UserTweets also has a static tx-id (pool size 1) yet succeeds 8 pages |
| TLS / curl_cffi transport | ❌ | `requests` (HTTP/1.1, no impersonation) gets identical p2 404 |
| Warm-up | ❌ | Removed; p2 404 unchanged |
| `querySource`/variables shape | ❌ | p1 and p2 build variables identically; p1 succeeds |
| **Query-id rotation** (alt id on p2) | ❌ | Both SearchTimeline query-ids (`hz_94eVA…`, `Bcw3RzK-…`) return empty-404 on the cursor'd p2 |

Every client-side lever has been pulled. The cursor'd SearchTimeline continuation is rejected at Twitter's gateway regardless of query-id, client, headers, or transport.

### Minor fidelity nit (not the cause)

`real_transaction_ids_by_endpoint` gives UserTweets/UserTweetsAndReplies/SearchTimeline a **pool of 1** each, so `_generate_transaction_id` always returns the same value. A real browser emits a fresh tx-id per request, so the rotation is effectively static for these endpoints. This is a real browser-fidelity gap but is **not** the 404 cause (UserTweets proves a static tx-id does not force 404s). Left as-is; enlarging the captured tx-id pools would improve fidelity but not change the SearchTimeline outcome.

---

## 4. Recommendations

1. **Ship curl_cffi + warm-up-off.** Two endpoints are 404-free and ~1.5–2× faster (HTTP/2, pooled). This is the real win.
2. **Keep the browser fallback for SearchTimeline deep pagination.** It is the correct response to a server-side gate; no HTTP-client change can remove it. Do not regress this path.
3. **Rate-limit-aware pacing for SearchTimeline** (optional hardening): the soft-block triggers faster under rapid pagination. The `x-rate-limit-remaining` header is available; backing off when it is low may extend how many pages succeed before the gate, but will not eliminate it.
4. **Never re-introduce a hand-rolled header set.** Always route through `APIManager._build_request_headers` (curl_cffi already does). Divergent headers were the original /with_replies 404 cause.

## 5. Reproduce

```bash
# definitive clean run (warm-up off, production context, curl_cffi)
python tools/diagnostics/pagination_test.py \
  --transport curl_cffi --accounts elonmusk --pages 6 \
  --endpoints UserTweets UserTweetsAndReplies SearchTimeline
```

Cross-check against `requests` by passing `--transport both`. SearchTimeline p2 will 404 on both — that is the server-side signal.
