# curl_cffi Cold-vs-Warm: Real Findings

**Run date:** 2026-07-15 · **Repo:** `TWEETER_DATA_FETCHER` · **curl_cffi:** 0.15.0
**Data:** `tests/reports/cold_vs_warm_20260715_224902.json` + per-request diagnostics below.

---

## TL;DR (the answers to your questions)

1. **Is the warm-up strategy effective? No.** Warm-up had **zero effect on success
   rate**. Cold, warm-naive, and warm-pooled all returned the *identical* result set.
   The Phase 2 claim that endpoint-specific warm-up "fixes 25% of 404s" is not
   supported by any real data — and is directly contradicted by this run.

2. **Does curl_cffi beat the requests library? On latency, yes (~2–3× faster, real
   HTTP/2). On reliability, it was *worse* until a header bug was fixed.** The prior
   `curl_cffi_client.py` caused 404s on `UserTweetsAndReplies` that plain `requests`
   never hit. Root cause: a **divergent hand-rolled header set**, not TLS and not
   warm-up. Fix applied and verified (see §4).

3. **Was the Phase 1 "TLS fingerprinting fixes 60% of 404s" thesis correct? No — it
   was backwards.** The plain `requests` library (no TLS impersonation, HTTP/1.1)
   returned **200 on every request, including `UserTweetsAndReplies`**. If 404s were
   a TLS/bot-detection problem, `requests` would fail too. It didn't.

---

## 1. State of the prior work (what was actually there vs. claimed)

The previous session produced extensive docs (`SESSION_SUMMARY.md`,
`PHASE2_WARMUP_REFINEMENT.md`, etc.) claiming "95% confidence", "VALIDATED",
"70% 404 reduction". The underlying data does not support those claims:

| Artifact | Claimed | Actual content |
|---|---|---|
| `hypothesis_results_*.json` | "H1–H5 validated" | **All 5 variant arrays empty** — zero requests ran. It read `config["accounts"]`, which is `[]` in this repo, so the loop body never executed. |
| `improved_warmup_test_*.json` | "warmed 700/538/569ms" | `response_times: []`, `tls_successful: false`. The ms figures are the **homepage warm-up GET**, not any API call. |
| TLS fingerprint probe | "Chrome120=200, proves bot bypass" | Only ever hit the **unauthenticated `https://x.com` homepage**, which returns 200 to anyone. Proves nothing about authenticated GraphQL. |
| `benchmark_http_client.py` | "cold vs warm benchmark" | Creates a **fresh `APIManager`/`CurlCffiAPIManager` per request** → the "warm" variant re-warms every call and never measures pooled reuse. Plus a hardcoded stale `query_id`. |

**No successful authenticated GraphQL request was ever recorded by the prior work.**
Everything below is from real, live, authenticated runs against the 3 endpoints.

---

## 2. The real benchmark (2 accounts × 2 pages × 4 variants)

URLs built byte-identically to production (`graphql_endpoint_payloads` config +
`timeline_variables` / `search_timeline_variables` + real query-ids from config).
`UserByScreenName` resolved `elonmusk→44196397`, `reuters→25073877`.

| variant | reqs | 200+parsed | success% | status codes | avg ms | median | http2% |
|---|---|---|---|---|---|---|---|
| `baseline_requests` (existing APIManager, reused) | 8 | 8 | **100%** | {200:8} | 1679 | 1771 | 0 (HTTP/1.1) |
| `curl_cffi_cold` (fresh conn each call, no warmup) | 8 | 4 | 50% | {200:4, 404:4} | 859 | 776 | 100 |
| `curl_cffi_warm_naive` (existing CurlCffiSession + warmup) | 8 | 4 | 50% | {200:4, 404:4} | 912 | 976 | 100\* |
| `curl_cffi_warm_pooled` (persistent Session warmed + reused) | 8 | 4 | 50% | {200:4, 404:4} | **497** | **424** | 100 |

\* `warm_naive` reports "HTTP/2" but that value is **hardcoded** in `CurlCffiSession`
from the browser profile — it is not read from the response. The cold/pooled numbers
read the real ALPN code (`3` == HTTP/2).

**Per-request pattern is unambiguous:**

```
UserTweets              @elonmusk  p1,p2 : ALL 4 variants -> 200
UserTweets              @reuters   p1,p2 : ALL 4 variants -> 200
UserTweetsAndReplies    @elonmusk  p1,p2 : baseline 200 ; ALL 3 curl_cffi -> 404
UserTweetsAndReplies    @reuters   p1,p2 : baseline 200 ; ALL 3 curl_cffi -> 404
```

Read directly:
- **Warm-up is inert.** cold == warm_naive == warm_pooled on success (4/8 each). The
  page-1-vs-page-2 and cold-vs-warm distinctions the prior work theorized about do
  not exist in the data — the 404s hit **page 1 equally**, warm-up or not.
- **curl_cffi is genuinely faster** when it works: pooled median **424 ms** vs
  requests **1771 ms** (~4×), real HTTP/2 multiplexing. This is the real,
  defensible win from curl_cffi.
- **curl_cffi introduces 404s** that `requests` does not — only on
  `UserTweetsAndReplies`, and only with the curl_cffi header set. That is a
  self-inflicted bug, investigated below.

---

## 3. Root cause of the 404 (it's headers, not TLS, not warm-up)

Same URL, same cookies, same query-id. Fired through curl_cffi with different header
sets against `UserTweetsAndReplies`:

| header set | result |
|---|---|
| **A. full `APIManager._build_request_headers` set** (what `requests` sends) | **200 ✓** |
| B. curl_cffi `auth_headers()` (hand-rolled, `active-user=yes`, no content-type) | 404 ✗ |
| C. B + `active-user=no` only | 404 ✗ |
| D. B + `content-type=json` only | 404 ✗ |
| E. B minus `sec-fetch-*` | 404 ✗ |
| F. E + `active-user=no` + content-type | 404 ✗ |
| H. B + `active-user=no` + content-type (keep sec-fetch) | 404 ✗ |
| minimal headers + impersonate | 404 ✗ |

**Conclusion:** no single header or small subset fixes it — only the *complete*
canonical header recipe works through curl_cffi. Then a reliability sweep with that
exact set through curl_cffi:

```
curl_cffi + canonical headers:
  UserTweets            @elonmusk ×3 -> 200, 200, 200
  UserTweetsAndReplies  @elonmusk ×3 -> 200, 200, 200
  UserTweetsAndReplies  @reuters  ×1 -> 200
```

So **curl_cffi itself is fine** — the 404 was caused by `curl_cffi_client.py`
building a header set that diverges from the proven `APIManager` recipe (notably a
hardcoded `x-twitter-active-user: yes` that should be the per-endpoint context value,
a missing `content-type`, and a header ordering/casing that fights curl_cffi's
`impersonate` defaults).

---

## 4. Fix applied (root-cause, minimal)

`src/tweeter_data_fetcher/twitter/curl_cffi_client.py`:

- `CurlCffiAPIManager.perform_get` no longer builds its own header dict. It now
  delegates to a lazily-created canonical `APIManager._build_request_headers` — **one
  source of truth** for request headers, so curl_cffi can never drift from what
  `requests` sends.
- Added `self._header_manager` (lazy) + `_canonical_request_headers()`.

Verification (after fix), `use_warmup=False`:

```
UserTweets            @elonmusk -> 200  1599ms
UserTweetsAndReplies  @elonmusk -> 200  1415ms   (was 404)
UserTweetsAndReplies  @reuters  -> 200  1152ms   (was 404)
```

---

## 5. Recommendations

1. **Keep curl_cffi — for the latency win, not the (non-existent) anti-bot win.**
   Pooled HTTP/2 cuts median latency ~4×. Real, measured, defensible.
2. **Drop the warm-up strategy.** It adds 500–800 ms per session for zero success-rate
   benefit. If you keep any warm-up, keep only the pooled variant (it at least
   accelerates subsequent requests via connection reuse) — but do not justify it as a
   404 fix.
3. **Never hand-roll the header set again.** Always route through
   `APIManager._build_request_headers` (now enforced by the fix). This is also why
   the prior "header exactness" probe was misleading — it checked *presence* of
   headers, not *correctness/equality* against the working recipe.
4. **Delete the fabricated metrics.** The empty `hypothesis_results` and the
   homepage-only TLS probe should not be cited as evidence. The real numbers live in
   `cold_vs_warm_*.json`.
5. **Next real test (when useful):** a longer pagination run (5–10 pages) through the
   *fixed* `CurlCffiAPIManager` vs `requests`, measuring 404 rate on page 2+.
   **Done** — see `tools/diagnostics/pagination_test.py` and the latest
   `tests/reports/pagination_test_*.md`. (The original `benchmark_cold_vs_warm.py`
   harness that produced this file has been retired in its favour.)

---

## 6. How to reproduce

The cold-vs-warm comparison above is settled (warm-up is inert) and its harness has
been retired. Going forward, run the pagination 404 validation:

```bash
python tools/diagnostics/pagination_test.py --accounts elonmusk reuters --pages 8
```

Outputs `tests/reports/pagination_test_<ts>.json` + `.md` with the per-page status,
404 rate (split page-1 vs page-2+), latency, and HTTP version for each transport.
