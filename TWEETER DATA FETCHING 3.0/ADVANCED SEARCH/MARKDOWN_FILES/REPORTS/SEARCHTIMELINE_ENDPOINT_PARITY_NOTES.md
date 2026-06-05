# SearchTimeline Endpoint Parity Notes

## Scope
Focused notes for browser-vs-replay parity checks for SearchTimeline.

## Tools
- `monitor_search_timeline.py` for static deterministic replay behavior
- browser devtools request snapshot (manual reference only)

## Key Checks
1. Query ID present and current.
2. `rawQuery` parity with intended search URL.
3. Features/variables/field toggles are coherent.
4. Session continuity behavior is preserved.
5. Distinguish first-request rejection from cursor-page degradation.

## Authoritative References
- `../TRANSPORT_RULES/TRANSPORT_RULES_AND_BEHAVIORAL_INVARIANTS.md`
- `../HISTORICAL_DEBUGGING/09_USERTWEETSANDREPLIES_QUERY_IDS_AND_CURSOR_LIFECYCLE.md`
- `../SEARCHTIMELINE/ROOT_CAUSE_ANALYSIS_SEARCHTIMELINE_404.md`
