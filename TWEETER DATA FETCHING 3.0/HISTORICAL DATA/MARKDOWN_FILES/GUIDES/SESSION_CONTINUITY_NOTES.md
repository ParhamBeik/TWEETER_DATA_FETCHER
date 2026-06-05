# Session and Auth Continuity Notes

This note summarizes session/auth behavior relevant to SearchTimeline and replies endpoints.

## Critical Points

1. Keep one stable `requests.Session` per monitor lifecycle.
2. Use coherent cookie jar from `config.json` (`api_cookies`).
3. Keep bearer token + `ct0` aligned with same authenticated account session.
4. Warmup route before sensitive GraphQL calls is part of transport behavior.
5. Avoid arbitrary per-request identity mutation.

## Files Involved
- `config.json`
- `setup_api_cookies.py`
- `api_manager.py`
- `monitor_search_timeline.py`

## Validation Source Documents
- `../TRANSPORT_RULES/TRANSPORT_RULES_AND_BEHAVIORAL_INVARIANTS.md`
- `../HISTORICAL_DEBUGGING/09_USERTWEETSANDREPLIES_QUERY_IDS_AND_CURSOR_LIFECYCLE.md`
- `../SEARCHTIMELINE/ROOT_CAUSE_ANALYSIS_SEARCHTIMELINE_404.md`
