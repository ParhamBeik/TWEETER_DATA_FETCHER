# Modular Monolith Refactor — TDD Evidence

## Source

User-provided modular monolith refactor plan implemented on July 14, 2026.

## Journeys

- Existing users can keep legacy imports and `python -m src...` commands during 4.x.
- Installed users can use canonical `tweeter_data_fetcher.*` imports and `tdf-*` commands.
- Operators can move local config to root `config/` without changing results or data paths.
- Pipelines preserve result/state/report schemas, rolling windows, endpoint order, and validation isolation.

## RED / GREEN

| Behavior | RED evidence | GREEN evidence |
|---|---|---|
| Canonical imports, CLIs, and config files | `pytest tests/unit/test_modular_monolith_compatibility.py -q` → 9 failures before package/config creation | Same target → 6 passed |
| Request-state persistence | `pytest ...::RequestStateTests -q` → missing `request_state` module | Focused request-state and HTTP tests → 8 passed |
| Full compatibility | Initial full run after package move → 2 legacy compatibility failures | Final `.venv/bin/python -m pytest -q` → 100 passed |

## Guarantees

| # | Guaranteed behavior | Test/command | Type | Result |
|---|---|---|---|---|
| 1 | Canonical and deprecated symbols import successfully | `tests/unit/test_modular_monolith_compatibility.py` | unit | PASS |
| 2 | Config precedence is explicit and templates remain byte-equivalent | `tests/unit/test_modular_monolith_compatibility.py` | unit | PASS |
| 3 | Canonical CLIs expose preserved flags | `tests/unit/test_modular_monolith_compatibility.py` | unit | PASS |
| 4 | Request parameter state preserves three-strike rule-out | `tests/unit/test_unified_historical_live_plan.py` | unit | PASS |
| 5 | Raw/processed/state paths and search isolation remain unchanged | storage and integration suites | unit/integration | PASS |
| 6 | Historical/live ordering and result schemas remain compatible | orchestration and pipeline suites | unit/integration | PASS |
| 7 | GraphQL variables, field toggles, cursors, and validation remain stable | contract suite | contract | PASS |
| 8 | Source, tests, and tools compile | `python -m compileall -q src tests tools` | compile | PASS |

## Notes

- No checkpoint commits were created because repository commits were not requested.
- Live smoke jobs require working external X/Twitter credentials and network behavior; their outcomes are recorded in the final handoff.
