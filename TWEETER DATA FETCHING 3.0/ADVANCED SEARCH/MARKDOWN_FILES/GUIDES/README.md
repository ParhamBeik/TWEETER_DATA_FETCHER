# MARKDOWN_FILES Index

This folder is the organized documentation hub for the project.

## Folder Map

### `TRANSPORT_RULES/`
- Authoritative transport behavior rules.
- Read before changing sessions, headers, query IDs, warmup, retries, pagination.
- Key file:
  - `TRANSPORT_RULES_AND_BEHAVIORAL_INVARIANTS.md`

### `HISTORICAL_DEBUGGING/`
- Chronological debugging history and endpoint recovery findings.
- Key files:
  - `09_USERTWEETSANDREPLIES_QUERY_IDS_AND_CURSOR_LIFECYCLE.md`
  - `04_TROUBLESHOOTING_V2.md`
  - `05_FETCHING_PIPELINE.md`

### `SEARCHTIMELINE/`
- SearchTimeline analysis notes retained for continuity.
- Historical context + prior investigations.

### `TESTING_GUIDES/`
- General operator guides.

### `ENDPOINT_ANALYSIS/`
- Endpoint-specific parity notes.

### `SESSION_AND_AUTH/`
- Session/cookie/auth continuity notes.

### `RATE_LIMITS/`
- Rate-limit operating notes.

### `VALIDATION_REPORTS/`
- Active runbooks and validation checklists.
- Primary active file:
  - `SEARCHTIMELINE_EXECUTION_AND_VALIDATION.md`

---

## Historically Critical Documents

1. `TRANSPORT_RULES/TRANSPORT_RULES_AND_BEHAVIORAL_INVARIANTS.md`
2. `HISTORICAL_DEBUGGING/09_USERTWEETSANDREPLIES_QUERY_IDS_AND_CURSOR_LIFECYCLE.md`
3. `HISTORICAL_DEBUGGING/04_TROUBLESHOOTING_V2.md`

These explain the request-lifecycle discoveries that stabilized the project.

---

## Current Authoritative SearchTimeline Workflow

Use static deterministic SearchTimeline flow only:
- `VALIDATION_REPORTS/SEARCHTIMELINE_EXECUTION_AND_VALIDATION.md`

Active system excludes Playwright/runtime injection during this phase.
