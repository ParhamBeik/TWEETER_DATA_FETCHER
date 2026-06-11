import sys


def main():
    modes = {"historical", "live", "search"}
    if len(sys.argv) < 2 or sys.argv[1] not in modes:
        print("Usage: python main.py [historical|live|search]")
        sys.exit(1)

    mode = sys.argv[1]
    try:
        if mode == "historical":
            from orchestrators.historical_runner import run_v4
            run_v4()
        elif mode == "live":
            from orchestrators.live_runner import main as live_main
            live_main()
        elif mode == "search":
            from search.search_runner import main as search_main
            search_main()
    except Exception as e:
        print(f"[ERROR] {mode} runner failed: {e}")
        raise


if __name__ == "__main__":
    main()
