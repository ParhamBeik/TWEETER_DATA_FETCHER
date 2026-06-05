# Rate Limit Operations

## SearchTimeline Baseline
- Configured endpoint budget is tracked per `SearchTimeline` in `api_manager.py`.
- Runtime state persists in `data/STATE/rate_limits.json`.

## Operational Guidance
1. During live multi-query tests, keep conservative delays.
2. Watch `x-rate-limit-remaining` and `x-rate-limit-reset` behavior.
3. If remaining approaches zero, pause testing until reset.
4. Prefer phased batches (5 → 10 → larger).

## Files to Monitor
- `data/STATE/rate_limits.json`
- `logs/endpoint_health.log`
- `logs/fetch_failures.log`

## Related Guide
- `../VALIDATION_REPORTS/SEARCHTIMELINE_EXECUTION_AND_VALIDATION.md`
