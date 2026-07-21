# Endpoint Request Templates & Pacing Reference Guide

**Repo:** `TWEETER_DATA_FETCHER` · **Subsystem:** `twitter_fetcher/` · **Updated:** July 2026

This document serves as the canonical technical reference for request construction, header rules, pacing guidelines, edge-gate avoidance mechanisms, and transport fallback policies across Twitter's GraphQL endpoints.

---

## 1. Overview of Endpoints & Transport Routing

| Endpoint | Primary Transport | Multi-Account Support | Multi-Page Support | Pacing / Gate Strategy |
|---|---|---|---|---|
| `UserTweets` | `curl_cffi` / `APIManager` | ✅ 100% | ✅ 5+ pages/account | Fast direct HTTP/2 multiplexing (0.4s inter-page sleep) |
| `UserTweetsAndReplies` | `curl_cffi` / `APIManager` | ✅ 100% | ✅ 4+ pages/account | Require `referer: https://x.com/{account}/with_replies`, 1.0s–1.5s inter-page delay, and 10s–12s inter-account cooldown |
| `SearchTimeline` | `APIManager` (P1) + Playwright SPA (P2+) | ✅ 100% | ✅ 5–6+ pages/query | Page 1 direct HTTP; Page 2+ (cursor-bearing) falls back to Playwright Chromium SPA context to bypass cursor 404 gate |

---

## 2. Endpoint Contract Templates

### 2a. `UserTweets`

- **Endpoint GraphQL Path:** `https://x.com/i/api/graphql/6r5OLCC_wFH4CpRyXKuAxA/UserTweets`
- **Referer Header:** `https://x.com/{username}` or `https://x.com/home`
- **Active User Header:** `x-twitter-active-user: yes`
- **Field Toggles:** `{"withArticlePlainText": false}`
- **GraphQL Variables Template:**
  ```json
  {
    "userId": "44196397",
    "count": 20,
    "includePromotedContent": true,
    "withQuickPromoteEligibilityTweetFields": true,
    "withVoice": true,
    "cursor": "DAAHCgABHNmF..." // Included on Page 2+
  }
  ```
- **Performance:** 100% success rate over `curl_cffi` with standard pacing.

---

### 2b. `UserTweetsAndReplies`

- **Endpoint GraphQL Path:** `https://x.com/i/api/graphql/klja8a2iJX_3to5RdfVlgw/UserTweetsAndReplies`
- **Referer Header (CRITICAL):** `https://x.com/{username}/with_replies`
- **Active User Header:** `x-twitter-active-user: yes`
- **Field Toggles:** `{"withArticlePlainText": false}`
- **GraphQL Variables Template:**
  ```json
  {
    "userId": "44196397",
    "count": 20,
    "includePromotedContent": true,
    "withCommunity": true,
    "withVoice": true,
    "cursor": "DAAHCgABHNmF..." // Included on Page 2+
  }
  ```
- **Edge-Gate Soft-Block Avoidance Rules:**
  - **Issue:** Firing > 4 rapid requests without an inter-account pause trips Twitter's session/IP token-bucket density gate, returning HTTP 404 Empty.
  - **Inter-Page Sleep:** 1.0s – 1.5s delay between pagination pages.
  - **Inter-Account Cooldown:** 10s – 12s sleep when switching target accounts in `historical` and `live` pipeline loops.
  - **Session Hygiene:** Re-initialize or refresh `CurlCffiSession` when transitioning between accounts.

---

### 2c. `SearchTimeline`

- **Endpoint GraphQL Path:** `https://x.com/i/api/graphql/hz_94eVAtrtQo_vO3my7Rw/SearchTimeline`
- **Field Toggles:** `None` (omitted)
- **Product Routing:**
  - **`Top` Product:** `https://x.com/search?q={query}&src=typed_query`
  - **`Latest` Product:** `https://x.com/search?q={query}&f=live&src=typed_query`
  - **`Trend Click` Route:** `https://x.com/search?q={query}&src=trend_click&vertical=trends`
- **GraphQL Variables Template:**
  ```json
  {
    "rawQuery": "Nvidia min_faves:100",
    "count": 20,
    "querySource": "typed_query",
    "product": "Latest",
    "withGrokTranslatedBio": false,
    "withQuickPromoteEligibilityTweetFields": false,
    "cursor": "DAADDAABCgAB..." // Included on Page 2+
  }
  ```
- **Browser Fallback Architecture:**
  - Page 1 requests succeed over HTTP (`APIManager`).
  - Page 2+ (cursor-bearing queries) sent via HTTP clients hit a server-side cursor gate (HTTP 404 Empty).
  - Production `SearchTimelineMonitor` (`pipelines/search/service.py`) automatically routes Page 2+ cursor queries to Playwright Chromium SPA context (`FetcherEngine.bootstrap_browser_context`), yielding **100% success across 5–6+ pages**.

---

## 3. Diagnostic Suite & Verification Commands

```bash
# Verify baseline contracts
python twitter_fetcher/diagnostics/verify_contract.py

# Multi-account/multi-page UserTweets and UserTweetsAndReplies pagination test
python twitter_fetcher/diagnostics/pagination_test.py

# SearchTimeline multi-route matrix diagnostic probe
python twitter_fetcher/diagnostics/probe_search_timeline_matrix.py --pages 6

# Browser-vs-Curl differential harness probe
python twitter_fetcher/diagnostics/probe_browser_vs_curl.py
```
