# main.py — unified entry point (stub, wired in Phase 2)
# Usage:
#   python main.py historical
#   python main.py live
#   python main.py search

import sys

MODE_MAP = {
    "historical": "orchestrators.historical_orchestrator",
    "live":       "orchestrators.live_orchestrator",
    "search":     "orchestrators.search_orchestrator",
}

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "historical"
    if mode not in MODE_MAP:
        print(f"Unknown mode '{mode}'. Choose: {list(MODE_MAP.keys())}")
        sys.exit(1)
    import importlib
    importlib.import_module(MODE_MAP[mode])
