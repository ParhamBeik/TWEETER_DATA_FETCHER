# Endpoint Transport Decision — 2026-08-10

Goal: pick the **fastest reliable** path per GraphQL endpoint, prove it against alternatives, wire it into production.

## Decision matrix

| Endpoint | Winning method | Rejected alternatives | Why faster + reliable | Evidence |
|---|---|---|---|---|
| **UserTweets** | `curl_cffi` HTTP, continuous pages (~1–1.5s/page) | Playwright; warmup GETs | 100% page success; browser adds minutes for same data | `pagination_test_20260810_215022`: 16/16 pages, 0×404, median ~1.1s |
| **UserTweetsAndReplies** | HTTP with **chunk every 2 pages** + session reset + 45–60s inter-account cool; escalate cool on empty 404; **no browser** | Continuous HTTP (dies ~p4–5); chunk=3 (~4 pages); browser fallback | Chunk=2 reaches 8 HTTP pages; browser is partial + slow | Probe 8/8; prod `_fetch_endpoint_result` 8/8 + second account 6/6 (`replies_prod_healthy_chunk2.json`, `replies_second_account_chunk2.json`) |
| **SearchTimeline** | **HTTP page 1**; Playwright for depth>1 (`http+browser`); **never retry HTTP on mid-pagination empty 404** | All-browser from page 1; HTTP p2+; long HTTP retry loops | p1 ~1–1.6s kept; browser only for deeper pages; p2 HTTP is guaranteed empty 404 | `SEARCHTIMELINE_404_ROOT_CAUSE.md`; hybrid in `pipelines/search/service.py` |

## Replies pacing (production)

Config keys under `anti_bot_simulation.delays_seconds`:

- `replies_chunk_pages`: **2**
- `between_pages_replies_*`: 2–3s
- `between_accounts_replies_*`: 45–60s
- `replies_retry_*`: 25–40s (escalates × attempt on density 404)

Pipeline behavior (`x_api/timeline.py`):

- Chunk cool + `reset_transport_session` every N pages
- Density empty 404 → cool/reset/retry; after attempts → `failed_replies_density_404` (**skips Playwright**)

## Search behavior (production)

- `pagination_depth` / cap **1**: HTTP only (browser only if initial 404)
- Cap **>1**: HTTP page 1 first, then Playwright for deeper pages (`http+browser`); full browser only if HTTP p1 fails
- Mid-pagination HTTP 404 with cursor → immediate `_cursor_gate` (no multi-minute HTTP retry)

## Replicability checklist

```bash
# UserTweets deep HTTP
.venv/bin/python twitter_fetcher/diagnostics/pagination_test.py \
  --transport curl_cffi --endpoints UserTweets --accounts chigrl --pages 8

# Replies chunked HTTP (needs healthy session; cool 2+ min after soft-block)
# Expect 8×200 with chunk=2 in live config / FetcherEngine

# Search p1 only
.venv/bin/python twitter_fetcher/diagnostics/pagination_test.py \
  --transport curl_cffi --endpoints SearchTimeline --pages 2
# Expect p1=200, p2=404 — do not “fix” with more HTTP retries
```

## Status

| Endpoint | Documented | Unit-tested policy | Live-proven | In main pipeline |
|---|---|---|---|---|
| UserTweets | yes | contract/pagination harness | yes | yes (HTTP) |
| UserTweetsAndReplies | yes | density skip + chunk default | yes (2026-08-10) | yes (chunk=2 + HTTP-only on density) |
| SearchTimeline | yes | cursor-gate + http+browser hybrid | p1 HTTP + p2 gate yes; depth browser may stall — pipeline keeps HTTP p1 | yes (HTTP depth-1; `http+browser` for depth>1 with HTTP salvage) |
