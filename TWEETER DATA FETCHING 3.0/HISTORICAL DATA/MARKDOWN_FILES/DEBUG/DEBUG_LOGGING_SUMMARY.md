# Debug Logging Implementation Summary

## What Was Implemented

A comprehensive debug logging system to diagnose SearchTimeline pagination failures (404 on page 2+).

## Files Created

### 1. `debug_logger.py` (NEW)
Standalone logging module with:
- **Dual-output logging**: Clean console (INFO) + detailed file logs (DEBUG)
- **Per-request dumps**: Full request/response snapshots saved as JSON
- **Secret masking**: Automatically masks sensitive headers (auth tokens, CSRF, cookies)
- **Per-run isolation**: Each execution gets timestamped folder under `logs/`

**Key functions**:
- `setup_logging(run_name, logs_root)` → Returns configured logger
- `dump_request(logger, page, ...)` → Saves request/response details

## Files Modified

### 2. `monitor_search_timeline_exact_replay.py` (MODIFIED)
Integrated debug logging throughout:

**Changes**:
- Added import: `from debug_logger import setup_logging, dump_request`
- Initialized logger at start of `_run_single_search()`: `log = setup_logging(name, Path("logs"))`
- Replaced all `print()` calls with `log.info()` in the method
- Added warmup request dump (page 0)
- Added 401 warmup warning
- Added GraphQL request dump (page 1+)
- Added exception logging

## Output Structure

When you run the script, you'll get:

```
logs/
  Iran_War_Brent_Gold_Inflation_Hormuz/
    20260529_193045/
      run.log                    ← Full timestamped trace
      requests/
        page_00.json             ← Warmup request details
        page_01.json             ← Page 1 request details
        page_02.json             ← Page 2 request details (with 404 body!)
```

## What You'll See

### Console Output (unchanged appearance)
```
[PHASE 1] SEARCH BUILD
  name: Iran_War_Brent_Gold_Inflation_Hormuz
  ...

[PHASE 2] SESSION INIT
  warmup_mode: search_page_once
  page=0 [OK ] status=200 tx_id=yes 1234ms
  warmup_status: 200
  ...

[PHASE 3] FIRST REQUEST
  page=1 [OK ] status=200 tx_id=yes 3064ms
  status=200 tweets_extracted=20 cursor_found=yes

[PHASE 4] PAGINATION
  page=2 [FAIL] status=404 tx_id=no 459ms
  └─ body: {"errors":[{"message":"Sorry, that page does not exist","code":34}]}
  └─ x-rate-limit-remaining: 180
```

### JSON Output (requests/page_02.json)
```json
{
  "page": 2,
  "request": {
    "method": "GET",
    "url": "https://x.com/i/api/graphql/099UqLkXma7fhT81Jv4n9g/SearchTimeline",
    "params": {...},
    "headers": {
      "authorization": "Bearer…[142 chars]",
      "x-csrf-token": "1bbc00…[128 chars]",
      "referer": "https://x.com/search?q=...",
      "user-agent": "Mozilla/5.0..."
    },
    "tx_id_present": false
  },
  "response": {
    "status": 404,
    "elapsed_ms": 459,
    "headers": {...},
    "body_preview": "{\"errors\":[{\"message\":\"Sorry, that page does not exist\",\"code\":34}]}"
  }
}
```

## Key Diagnostic Features

1. **404 Root Cause**: Response body now visible in console and JSON
2. **tx-id Verification**: `tx_id_present` field confirms whether header was sent
3. **Warmup 401 Detection**: Explicit warning when auth is stale
4. **Rate Limit Visibility**: Shows remaining requests when hitting limits
5. **Request Timing**: Elapsed time in milliseconds for each request
6. **Full Audit Trail**: Complete request/response history in JSON files

## Security

All sensitive headers are automatically masked:
- `authorization` → `Bearer…[142 chars]`
- `x-csrf-token` → `1bbc00…[128 chars]`
- `cookie` → masked
- `x-client-transaction-id` → masked

Only first 6 characters + length shown in logs.

## Next Steps

Run the script and check:
1. Console output for immediate feedback
2. `logs/{search}/{timestamp}/run.log` for full trace
3. `logs/{search}/{timestamp}/requests/page_02.json` for 404 details

The response body in page_02.json will tell you exactly why X.com rejected the request.
