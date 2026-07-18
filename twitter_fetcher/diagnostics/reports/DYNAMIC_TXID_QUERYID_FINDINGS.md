# Dynamic `x-client-transaction-id` & Query-Id Discovery — Hypothesis Test

**Run date:** 2026-07-17 · **Repo:** `TWEETER_DATA_FETCHER` · **Transport:** `curl_cffi` 0.15.0 (`impersonate=chrome120`, HTTP/2) · **Auth:** single logged-in session from `config.json` · **No production code was modified.**

This tests the two levers a body of external research (XClientTransaction, twitter-graphql-scraper, twitter-cli, tweetxvault) names as *the* solution for X's GraphQL 404s:

1. **H1 — dynamic transaction id:** generate a correct, per-request, path-specific `x-client-transaction-id` via the reverse-engineered algorithm (`xclienttransaction`), instead of the production static pool-of-1.
2. **H2 — dynamic query-id discovery:** scrape the *current* `queryId`s from X's live JS bundles, instead of the hardcoded config values.

Primary data: `tests/reports/dynamic_txid_20260717_173310.{json,md}` (5 target accounts × 5 pages × A/B on UserTweets + UserTweetsAndReplies), plus the live SearchTimeline 4-way and query-id cross-check below.

---

## TL;DR — both hypotheses DISCONFIRMED as operational levers

| Lever | Verdict | Evidence |
|---|---|---|
| **H1 dynamic tx-id** | ❌ no benefit | Valid ids are accepted on UserTweets/UTAR (A=B=100%), but **do not touch the SearchTimeline or UTAR anti-abuse gates** — omit/static/dynamic/fake tx-id all 404 identically once a gate is active. |
| **H2 query-id discovery** | ❌ redundant | Config query-ids **exactly match** the live `main.js` query-ids for all 4 endpoints. The existing refresh already keeps them current. |

The research tools **work** (they produce valid values) but **do not help** this project: the gates they were supposed to bypass are **not** tx-id- or query-id-gated. They are account-level, request-density soft-blocks — exactly what prior work (`SEARCHTIMELINE_404_ROOT_CAUSE.md`) already established, now re-confirmed against the one tx-id variant that had never been tested: a *correctly computed* one.

**Recommendation:** do **not** adopt dynamic tx-id generation or live query-id scraping into the pipeline. Both add cost for zero success-rate gain. The real levers — request-context fix, rate-aware pacing, and the Playwright browser fallback — are already in place.

---

## 1. Setup — the toolchain is genuinely functional

`xclienttransaction` 1.0.3 was installed and validated end-to-end:

- It produces **94-char, per-request-distinct, path+time-specific** ids — the real format (the current code's `_fake_id_like_current_code` is also 94 chars but random; the static pool holds real captured values).
- `ClientTransaction` must be **seeded from the logged-in home page** (`https://x.com/home` with cookies): the `ondemand.s` chunk map and `twitter-site-verification` key only exist there. The logged-out page has neither. One ~270 KB home fetch + one ~18 KB ondemand fetch seed the generator for the whole run (~1.6 s).
- The library's ondemand URL template (`responsive-web/client-web/ondemand.s.<hash>a.js`) still resolves 200.

So the mechanism is real and reproducible. The question is whether it changes outcomes.

## 2. H1 — dynamic tx-id does NOT bypass the gates

### 2a. SearchTimeline — the 4-way test (decisive)

Fresh queries, page 1, four tx-id strategies through the **same production code path** (`CurlCffiAPIManager`):

| request # | tx-id strategy | query | result | rate-remaining |
|---|---|---|---|---|
| 1 | **omit** | Nvidia | **200** ✓ | 39 |
| 2 | static pool | Nvidia | 404 | 38 |
| 3 | **dynamic (computed)** | Nvidia | 404 | 37 |
| 4 | fake random | Nvidia | 404 | 36 |
| 5 | omit | Apple | 404 | 35 |
| 6+ | *(any)* | *(any)* | 404 | … |

**The first SearchTimeline request in a cooldown window returns 200; every subsequent one 404s, identically, regardless of whether the tx-id is omitted, static, dynamically computed, or fake.** All 404s decrement the rate counter (39→24), i.e. auth is accepted and the request is throttled-then-rejected — the soft-block signature.

This means the "A=200, B=404" pattern seen in an early A/B run was a **positional artifact** (A fired first, B second). The gate is order/density based and **tx-id-agnostic**.

### 2b. UserTweetsAndReplies — same pattern, exposed by the per-request log

The A/B sweep's aggregate looked damning (`A/UTAR 44%` vs `B/UTAR 0%`), but the per-request sequence proves it is positional, not tx-id:

```
A elonmusk        p1→p4 : 200, 200, 200, 200   (rate 499→496)
A elonmusk        p5    : 404                   (rate 495)  ← gate trips here
B elonmusk        p1    : 404                   (rate 494)
A Reuters         p1    : 404                   (rate 493)  ← STATIC tx-id also 404s
B Reuters         p1    : 404                   (rate 492)
A realDonaldTrump p1    : 404                   (rate 491)  ← STATIC tx-id also 404s
B realDonaldTrump p1    : 404                   (rate 490)
… (all subsequent A and B 404, rate 489→486)
```

Once the UTAR gate trips (after ~4 rapid requests), **A (static) 404s just as much as B (dynamic)**. The aggregate lied because B always ran second, inside the gated window. This is precisely the trap the prior session fell into; the per-request log is the truth.

> **New nuance beyond prior work:** UTAR does not only have the (already-fixed) page-1 *context* bug — it *also* has a SearchTimeline-style positional soft-block that trips on rapid request density (~4–5 quick requests). It is neither context-fixable nor tx-id-fixable; it is density-driven. This matches `SEARCHTIMELINE_404_ROOT_CAUSE.md` recommendation #3 (rate-aware pacing).

### 2c. UserTweets — clean baseline, A = B = 100%

UserTweets has no gate under this load. Both variants paginate 5/5 pages across all 5 accounts:

| variant/endpoint | reqs | success% | 404 | p2+ success | max page | median ms |
|---|---|---|---|---|---|---|
| `A/UserTweets` (static) | 25 | **100.0** | 0 | 20/20 | 5 | 829 |
| `B/UserTweets` (dynamic) | 25 | **100.0** | 0 | 20/20 | 5 | 840 |

Dynamic tx-id is a drop-in with **zero regression and zero improvement** — a fidelity nicety, not a fix.

## 3. H2 — query-id discovery is redundant (config already live)

Live query-ids scraped from `main.0c53df8a.js` (1.0 MB, fetched from the logged-in home) vs. `config.json`:

| operation | config | live main.js | verdict |
|---|---|---|---|
| UserTweets | `6r5OLCC_wFH4CpRyXKuAmQ` | `6r5OLCC_wFH4CpRyXKuAmQ` | ✅ MATCH |
| UserTweetsAndReplies | `klja8a2iJX_3to5RdfVlgw` | `klja8a2iJX_3to5RdfVlgw` | ✅ MATCH |
| SearchTimeline | `hz_94eVAtrtQo_vO3my7Rw` | `hz_94eVAtrtQo_vO3my7Rw` | ✅ MATCH |
| UserByScreenName | `2qvSHpkWTMS9i0zUejRCOuH5A` | `2qvSHpkWTMS9i0zUejRCOuH5A` | ✅ MATCH |

(Production `config.json` query-ids differ from `config.example.json` — direct evidence the existing refresh mechanism is doing its job.) Since the ids are already current, dynamically re-scraping them would change nothing — and the SearchTimeline gate fires even with the correct current id.

## 4. Cost of adopting the research tools (if one ignored the verdict)

| item | static pool (today) | dynamic (research) |
|---|---|---|
| per-session setup | none | +1 logged-in home fetch (~270 KB) + 1 ondemand fetch (~18 KB), ~1.6 s |
| per-request | O(1) dict lookup | hash + base64 per request (cheap) |
| maintenance | refresh pool via existing Playwright capture | track X bundle-layout changes (the ondemand chunk map already moved HTML→logged-in-home; the lib's regexes are one UI rewrite from breaking) |
| success-rate delta | — | **0** |

## 5. What actually moves the needle (confirmed, not new)

1. **Request context** — `x-twitter-active-user: yes` + tab referer (already shipped; fixes UTAR page-1).
2. **Request-density pacing** — the SearchTimeline *and* UTAR soft-blocks trip on rapid sequential requests. Backing off (inter-page sleep, and/or `x-rate-limit-remaining`-aware pacing) extends how many pages succeed before the gate. This is the only untested lever with upside, and it is pacing, not tx-id.
3. **Playwright browser fallback for SearchTimeline deep pagination** — the correct response to a server-side gate that no HTTP-client change can remove.

## 6. Reproduce

```bash
# install the research lib
pip install xclienttransaction

# A/B sweep on the user endpoints (rate-paced), 5 accounts × 5 pages
python tools/diagnostics/probe_dynamic_txid.py \
  --endpoints UserTweets UserTweetsAndReplies \
  --accounts elonmusk Reuters realDonaldTrump TankerTrackers KobeissiLetter \
  --pages 5

# SearchTimeline / UTAR are gated on density — a fast multi-account sweep will
# mostly 404 for BOTH variants. To see the gate's tx-id-agnosticism directly,
# re-run the 4-way (omit/static/dynamic/fake) inline probe documented in §2a.
```

## 7. Methodological warning

Naive A/B aggregates over gated endpoints **lie**: whichever variant runs first eats the gate's "first request succeeds" budget, and the second variant inherits the soft-block. Always read the **per-request sequence with the rate counter** — the rate counter decrementing on a 404 is the tell that auth was accepted and the request was throttled. Aggregate success% without that sequence is how the prior session fabricated its "validated" conclusions.
