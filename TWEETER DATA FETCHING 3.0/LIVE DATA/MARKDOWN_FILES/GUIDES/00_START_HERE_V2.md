# 00 - Start Here

This project is a hybrid X/Twitter intelligence collector. It preserves the newer system's storage, dedupe, endpoint comparison, snapshots, viral detection, and live scheduling, while keeping transport behavior aligned with the real X/Twitter frontend runtime.

The most important project rule is simple:

Behavioral parity with the real working browser-derived flow is more important than architectural elegance.

The previous regression happened because the code became cleaner but stopped behaving like the real frontend runtime. Later runtime evidence clarified the cause: the hybrid transport architecture was not fundamentally broken, but its request behavior diverged in the places X validates most strongly: query ID freshness, session continuity, first replies request lifecycle, and cursor validity.

## Required Reading Order

1. `TRANSPORT_RULES_AND_BEHAVIORAL_INVARIANTS.md`
2. `09_USERTWEETSANDREPLIES_QUERY_IDS_AND_CURSOR_LIFECYCLE.md`
3. `01_SETUP_AND_RUN.md`
4. `02_MINIMAL_SCRIPTS_GUIDE.md`
5. `03_OUTPUT_FORMAT_STANDARD.md`
6. `04_TROUBLESHOOTING_V2.md`
7. `05_FETCHING_PIPELINE.md`
8. `06_STORAGE_ENDPOINTS_DEDUPE.md`
9. `07_LIVE_MONITORING_AND_VIRAL.md`
10. `08_EXTENSION_GUIDE.md`

Read the transport rules first. Do not start refactoring from module boundaries. Start from runtime behavior.

## System Shape

Runtime code lives in the project root:

- `api_manager.py`: session, headers, request execution, query IDs, rate-limit state, endpoint health.
- `fetch_historical_tweets_hybrid.py`: historical fetching, endpoint calls, parsing, conversation extraction.
- `storage_manager.py`: output folders, formatting, endpoint comparison, dedupe registry, snapshots.
- `monitor_live_tweets_hybrid.py`: live polling, account tiers, snapshot creation, viral checks.
- `viral_detector.py`: snapshot-based scoring.
- `setup_api_cookies.py`: config bootstrap from browser cookies, bearer token, transaction ID, query IDs.
- `config.json`: durable runtime configuration.

## Evidence Categories

The docs separate four categories:

- Confirmed truths: proven by old working behavior and latest successful runtime evidence.
- Experimental findings: observed during browser/runtime debugging, useful but not universal law.
- Assumptions: plausible explanations that still need browser capture or repeated runtime proof.
- Unresolved bottlenecks: known weak spots that can still fail under X/Twitter changes.

Do not promote an assumption into an invariant without evidence.

## Source Of Truth

The old experimental project remains the behavioral baseline for reliable fetching:

`/Users/parham/Downloads/PERSONAL PROJECTS/EXPERIMENTS/TWEETER DATA FETCHING`

Especially:

- `fetch_historical_tweets.py`
- `monitor_live_tweets.py`
- `setup_api_cookies.py`

The copy project is the active hybrid system. Do not modify the experimental source when making changes here.

Latest successful runtime evidence adds an important refinement: the active hybrid architecture can work when it matches the real frontend's behavior closely enough. The architecture itself was not the root cause; behavior drift was.

## Non-Negotiable Principle

If a proposed change makes transport cleaner but changes the first successful `UserTweetsAndReplies` request lifecycle, it is probably wrong.

The key health signal is not "does pagination eventually 404?" Pagination 404 can be normal after successful retrieval. The key health signal is:

The first `UserTweetsAndReplies` request for an account must become valid and return data. Later cursor-driven 404s are a different class of event and must not be treated as proof that the endpoint is broken.
