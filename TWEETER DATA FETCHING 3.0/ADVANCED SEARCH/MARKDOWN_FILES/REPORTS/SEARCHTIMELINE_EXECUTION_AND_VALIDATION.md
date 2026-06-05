# SearchTimeline Static Stabilization Validation

This guide is for the **static deterministic requests.Session** SearchTimeline system.

## Scope

Active scripts:
- `monitor_search_timeline.py`

Required data/config:
- `config.json`
- `search_config.json`
- `REFRENCES FILES/SearchTimeline.txt` (for local parser validation)

Not used in this static phase:
- Playwright/runtime bootstrap systems
- browser request interception tools
- batch replay experiments

---

## Static Lifecycle (Expected)

1. Start one stable `requests.Session` via `APIManager`.
2. Load cookies/bearer/csrf/tx-id from `config.json`.
3. Warmup `https://x.com/search?q=...`.
4. Send first SearchTimeline request with static query-id, no cursor.
5. Parse entries + bottom cursor.
6. Paginate with cursor on same session.
7. Save raw + parsed outputs, dedupe by tweet ID.

---

## Commands (Exact)

From anywhere:

```bash
python3 "/Users/parham/Downloads/PERSONAL PROJECTS/TWEETER DATA FETCHING 3.0/monitor_search_timeline.py" --once --dry-run --name Iran_War_Brent_Gold_Inflation_Hormuz
```

Then live one-cycle:

```bash
python3 "/Users/parham/Downloads/PERSONAL PROJECTS/TWEETER DATA FETCHING 3.0/monitor_search_timeline.py" --once --name Iran_War_Brent_Gold_Inflation_Hormuz
```

Optional parser-only validation from reference payload:

```bash
python3 "/Users/parham/Downloads/PERSONAL PROJECTS/TWEETER DATA FETCHING 3.0/monitor_search_timeline.py" --validate-reference "/Users/parham/Downloads/PERSONAL PROJECTS/TWEETER DATA FETCHING 3.0/REFRENCES FILES/SearchTimeline.txt" --once --name Iran_War_Brent_Gold_Inflation_Hormuz
```

---

## Success Signals

First request success indicators:
- `Warmup status: 200` (or other 2xx/3xx)
- `First SearchTimeline request status: 200`
- `Cursor extracted: yes` or `Cursor extracted: no` after valid parse
- `Raw pages stored: 1+`

Pagination success indicators:
- Page lines show cursor progression (`cursor=... -> ...`)
- `timestamps newest=... | oldest=...`
- Parsed files written:
  - `data/SEARCH_TIMELINE/<search>/<product>/PARSED/*.json`
  - `data/SEARCH_TIMELINE/<search>/<product>/PARSED/*.txt`

---

## Failure Signals

First request rejection indicators:
- Repeated `404 on SearchTimeline - request context or query ID rejected`
- `First request failed before any page parse. status=404 health=context_rejected`
- `Raw pages stored: 0`

Pagination degradation indicators (after initial success):
- Cursor-related stop reasons (`dead_cursor_404`, repeated cursor detection, no bottom cursor)
- This is not automatically full endpoint failure.

---

## Logs to Inspect

- `logs/fetch_failures.log`
- `logs/endpoint_health.log`
- `logs/cursor_exhausted.log`
- `logs/cursor_recovery.log`
- `logs/repeated_cursor_detected.log`

State files:
- `data/STATE/rate_limits.json`
- `data/STATE/endpoint_health.json`

---

## Rate-Limit Safety

- SearchTimeline budget is tracked by response headers.
- If remaining drops quickly, stop repeated tests and wait for reset.
- Prefer one-cycle tests while stabilizing first-request behavior.
