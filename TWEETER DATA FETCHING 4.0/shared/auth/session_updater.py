#!/usr/bin/env python3
"""
Twitter Session Updater

Refreshes authentication parameters (cookies, x-client-transaction-id,
query IDs) by opening an interactive browser session where the user can
log in. Uses Playwright to intercept fresh parameters from the live
Twitter session.

Usage:
    python3 session_updater.py

Options:
    1. Quick refresh  - Inject existing cookies, hope they still work
    2. Full login     - Open a visible browser for manual login (recommended)
"""

import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from playwright.sync_api import Request, sync_playwright

logger = logging.getLogger(__name__)

# Default query IDs (will be overwritten if fresh IDs are intercepted)
DEFAULT_QUERY_IDS: Dict[str, str] = {
    "user_by_screen_name_query_id": "sLVLhk0bGj3MVFEKTdax1w",
    "user_tweets_query_id": "pQHADmT91zIY83UbK0x4Lw",
    "user_tweets_and_replies_query_id": "xdqXQQg4vOBF9Np6VtUsdw",
    "tweet_detail_query_id": "",
    "search_timeline_query_id": "099UqLkXma7fhT81Jv4n9g",
}

# Maps GraphQL endpoint names to config.json key names
ENDPOINT_KEY_MAP: Dict[str, str] = {
    "UserByScreenName": "user_by_screen_name_query_id",
    "UserTweets": "user_tweets_query_id",
    "UserTweetsAndReplies": "user_tweets_and_replies_query_id",
    "TweetDetail": "tweet_detail_query_id",
    "SearchTimeline": "search_timeline_query_id",
}


class SessionUpdater:
    """
    Refreshes Twitter authentication parameters via Playwright.

    Two modes:
      QUICK  - injects existing cookies from config.json, intercepts fresh
               parameters from GraphQL requests. Works only if the current
               session is still valid.
      FULL   - opens a headed (visible) browser so the user can log in
               manually.  All parameters are then extracted from the fresh
               session.  Recommended when cookies have expired.
    """

    def __init__(self) -> None:
        self.config_path = (
            Path(__file__).resolve().parents[1] / "config" / "config.json"
        )
        self._target_url = "https://x.com/home"
        self._graphql_indicator = "/graphql/"

    # ------------------------------------------------------------------ #
    #  Config I/O
    # ------------------------------------------------------------------ #

    def _load_config(self) -> Dict[str, Any]:
        """Load shared/config/config.json."""
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Config file not found: {self.config_path}"
            )
        with open(self.config_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save_config(self, cfg: Dict[str, Any]) -> None:
        """Persist the updated config to disk with atomic write and backup."""
        if self.config_path.exists():
            backup_path = self.config_path.with_suffix(".json.bak")
            shutil.copy2(self.config_path, backup_path)
            logger.info("Config backup saved to %s", backup_path)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self.config_path.parent, suffix=".tmp"
        )
        try:
            with open(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2, ensure_ascii=False)
            Path(tmp_path).replace(self.config_path)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise
        logger.info("Config saved to %s", self.config_path)

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _cookiestring_to_playwright(
        cookies_dict: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        """Convert a flat {name: value} dict into Playwright cookie format."""
        return [
            {
                "name": name,
                "value": str(value),
                "domain": ".x.com",
                "path": "/",
            }
            for name, value in cookies_dict.items()
        ]

    def _extract_query_id_from_url(
        self, url: str
    ) -> Optional[tuple[str, str]]:
        """Extract (endpoint, query_id) from a GraphQL request URL."""
        try:
            parsed = urlparse(url.strip())
            parts = [p for p in (parsed.path or "").split("/") if p]
            if "graphql" not in parts:
                return None
            graph_idx = parts.index("graphql")
            if len(parts) <= graph_idx + 2:
                return None
            query_id = parts[graph_idx + 1]
            endpoint = parts[graph_idx + 2]
            if endpoint in ENDPOINT_KEY_MAP and query_id:
                return (endpoint, query_id)
        except Exception:
            pass
        return None

    @staticmethod
    def _apply_extracted(
        cfg: Dict[str, Any],
        extracted: Dict[str, Any],
        old_txid: Optional[str],
        old_cookies: Dict[str, str],
    ) -> Dict[str, str]:
        """Merge extracted parameters into config and return a change report."""
        report: Dict[str, str] = {}

        if extracted.get("ct0") and cfg.get("api_cookies"):
            cfg["api_cookies"]["ct0"] = extracted["ct0"]
            report["ct0"] = "updated" if extracted["ct0"] != old_cookies.get("ct0") else "unchanged"

        if extracted.get("auth_token") and cfg.get("api_cookies"):
            cfg["api_cookies"]["auth_token"] = extracted["auth_token"]

        if extracted.get("x_client_transaction_id"):
            cfg.setdefault("api_headers", {})
            cfg["api_headers"]["x-client-transaction-id"] = extracted["x_client_transaction_id"]
            report["x-client-transaction-id"] = "new" if extracted["x_client_transaction_id"] != old_txid else "unchanged"

        if extracted.get("query_ids"):
            api_config = cfg.setdefault("api_config", {})
            api_config.update(extracted["query_ids"])
            report["query_ids"] = str(len(extracted["query_ids"]))

        return report

    # ------------------------------------------------------------------ #
    #  Mode 1 - Quick Refresh (headless, injects existing cookies)
    # ------------------------------------------------------------------ #

    def quick_refresh(self, cfg: Dict[str, Any]) -> bool:
        """
        Attempt a quick refresh by injecting existing cookies from config.json.

        Returns True if at least one parameter was updated.
        """
        current_cookies = cfg.get("api_cookies", {})
        if not current_cookies:
            print("\nNo cookies in config.json. Use mode 2 (Full login) instead.\n")
            return False
        if not current_cookies.get("auth_token") or not current_cookies.get("ct0"):
            print(
                "\nWarning: config.json is missing critical cookies"
                " (auth_token / ct0). Quick refresh may fail.\n"
            )

        old_txid = cfg.get("api_headers", {}).get("x-client-transaction-id")
        old_ct0 = current_cookies.get("ct0")

        playwright_cookies = self._cookiestring_to_playwright(current_cookies)

        extracted: Dict[str, Any] = {
            "x_client_transaction_id": None,
            "ct0": old_ct0,
            "auth_token": current_cookies.get("auth_token"),
            "query_ids": {},
        }

        logger.info("Launching Playwright for quick cookie refresh...")
        print("\nLaunching browser with existing cookies...")

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context()
                ctx.add_cookies(playwright_cookies)
                page = ctx.new_page()

                def _on_request(req: Request) -> None:
                    url = req.url
                    headers = req.headers

                    # Capture x-client-transaction-id
                    if (
                        self._graphql_indicator in url
                        and "x-client-transaction-id" in headers
                        and not extracted["x_client_transaction_id"]
                    ):
                        extracted["x_client_transaction_id"] = headers[
                            "x-client-transaction-id"
                        ]
                        logger.debug("Intercepted x-client-transaction-id")

                    # Extract query IDs
                    if self._graphql_indicator in url:
                        result = self._extract_query_id_from_url(url)
                        if result:
                            endpoint, query_id = result
                            key = ENDPOINT_KEY_MAP.get(endpoint)
                            if key:
                                extracted["query_ids"][key] = query_id

                page.on("request", _on_request)
                page.goto(self._target_url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(5)

                # Read back updated cookies
                for cookie in ctx.cookies():
                    if cookie["name"] == "ct0":
                        extracted["ct0"] = cookie["value"]
                    elif cookie["name"] == "auth_token":
                        extracted["auth_token"] = cookie["value"]

                browser.close()

        except KeyboardInterrupt:
            logger.info("Quick refresh cancelled by user.")
            print("\nCancelled.\n")
            return False
        except Exception as exc:
            logger.error("Quick refresh failed: %s", exc)
            print(f"\nQuick refresh failed: {exc}\n")
            print("Your cookies may have expired. Use mode 2 (Full login) instead.\n")
            return False

        if extracted["x_client_transaction_id"] or extracted["ct0"] != old_ct0 or extracted["query_ids"]:
            report = self._apply_extracted(cfg, extracted, old_txid, current_cookies)
            self._save_config(cfg)

            print("\nQuick refresh completed!")
            print(f"  x-client-transaction-id: [{report.get('x-client-transaction-id', 'unchanged')}]")
            print(f"  ct0:                     [{report.get('ct0', 'unchanged')}]")
            print(f"  Query IDs extracted:     {report.get('query_ids', '0')}")
            return True

        logger.error("Quick refresh returned no fresh parameters. Session expired.")
        print("\nNo fresh parameters captured. Session likely expired.\n")
        print("Use mode 2 (Full login) instead.\n")
        return False

    # ------------------------------------------------------------------ #
    #  Mode 2 - Full Interactive Login (headed, user logs in manually)
    # ------------------------------------------------------------------ #

    def full_login(self, cfg: Dict[str, Any]) -> bool:
        """
        Open a visible browser for the user to log in to X/Twitter manually.

        Extracts ALL fresh parameters: cookies, x-client-transaction-id,
        and query IDs.  Recommended when existing cookies have expired.
        """
        existing_cookies = cfg.get("api_cookies", {})
        old_txid = cfg.get("api_headers", {}).get("x-client-transaction-id")

        pw_cookies = self._cookiestring_to_playwright(existing_cookies) if existing_cookies else []

        extracted: Dict[str, Any] = {
            "x_client_transaction_id": None,
            "ct0": None,
            "auth_token": None,
            "all_cookies": {},
            "query_ids": {},
        }

        logger.info("Launching Playwright for full interactive login...")

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=False)  # visible browser
                ctx = browser.new_context()

                if pw_cookies:
                    ctx.add_cookies(pw_cookies)  # help pre-fill session

                page = ctx.new_page()

                def _on_request(req: Request) -> None:
                    url = req.url
                    headers = req.headers

                    if (
                        self._graphql_indicator in url
                        and "x-client-transaction-id" in headers
                        and not extracted["x_client_transaction_id"]
                    ):
                        extracted["x_client_transaction_id"] = headers[
                            "x-client-transaction-id"
                        ]
                        logger.debug("Intercepted x-client-transaction-id")

                    if self._graphql_indicator in url:
                        result = self._extract_query_id_from_url(url)
                        if result:
                            endpoint, query_id = result
                            key = ENDPOINT_KEY_MAP.get(endpoint)
                            if key:
                                extracted["query_ids"][key] = query_id

                page.on("request", _on_request)

                # ---------------------------------------------------------- #
                #  Interactive login prompt
                # ---------------------------------------------------------- #
                print("\n" + "=" * 60)
                print("  Browser window will open shortly.")
                print("  Please log in to your X (Twitter) account.")
                print("=" * 60)
                print()
                print("After logging in:")
                print("  1. Navigate to x.com/home (or any page)")
                print("  2. Wait for the page to fully load")
                print("  3. Do NOT close the browser yet")
                print()
                print("Waiting up to 120 seconds for you to log in...")
                print("(Press Ctrl+C at any time to cancel)\n")

                page.goto(self._target_url, wait_until="domcontentloaded", timeout=60000)

                # Poll for home page to confirm session is active
                login_timeout = 120
                start = time.time()
                while time.time() - start < login_timeout:
                    try:
                        url = page.url
                        if "home" in url.lower() or "x.com" in url.lower():
                            time.sleep(3)  # let initial requests fire
                            break
                    except Exception:
                        pass
                    time.sleep(2)

                # Extra wait for all network activity
                print("Session active. Waiting for data extraction...")
                time.sleep(10)

                # Read ALL fresh cookies
                for cookie in ctx.cookies():
                    extracted["all_cookies"][cookie["name"]] = cookie["value"]

                browser.close()

        except KeyboardInterrupt:
            logger.info("Full login cancelled by user.")
            print("\nCancelled.\n")
            return False
        except Exception as exc:
            logger.error("Full login failed: %s", exc)
            print(f"\nFull login failed: {exc}\n")
            return False

        # ---------------------------------------------------------- #
        #  Apply extracted data to config
        # ---------------------------------------------------------- #
        if not extracted["all_cookies"]:
            logger.error("Full login captured no cookies.")
            print("\nNo cookies captured. Try again.\n")
            return False

        critical_missing = [
            key
            for key in ("auth_token", "ct0")
            if key not in extracted["all_cookies"]
        ]
        if critical_missing:
            logger.warning(
                "Full login captured cookies but missing critical keys: %s",
                critical_missing,
            )
            print(
                f"\nWarning: captured cookies are missing critical keys:"
                f" {', '.join(critical_missing)}."
                f"\nThe session may not work. Saving anyway.\n"
            )

        # Replace all cookies
        cfg["api_cookies"] = extracted["all_cookies"]

        if extracted["x_client_transaction_id"]:
            cfg.setdefault("api_headers", {})
            cfg["api_headers"]["x-client-transaction-id"] = extracted[
                "x_client_transaction_id"
            ]

        if extracted["query_ids"]:
            api_config = cfg.setdefault("api_config", {})
            api_config.update(extracted["query_ids"])

        self._save_config(cfg)

        report = {}
        report["cookies"] = str(len(extracted["all_cookies"]))
        report["x-client-transaction-id"] = (
            "fresh" if extracted["x_client_transaction_id"] else "not captured"
        )
        report["query_ids"] = str(len(extracted["query_ids"]))

        print("\n" + "=" * 60)
        print("  Full login refresh completed successfully!")
        print("=" * 60)
        print(f"\n  Cookies updated:           {report['cookies']}")
        print(f"  x-client-transaction-id:   [{report['x-client-transaction-id']}]")
        print(f"  Query IDs extracted:       {report['query_ids']}")

        if extracted["query_ids"]:
            for key, val in extracted["query_ids"].items():
                print(f"    - {key}: {val}")

        print(f"\n  Config saved to: {self.config_path}\n")
        return True

    # ------------------------------------------------------------------ #
    #  CLI entry point
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Interactive CLI: choose quick refresh or full login mode."""
        print("\n" + "=" * 60)
        print("  Twitter Session Updater")
        print("=" * 60)
        print()
        print("Choose a refresh mode:")
        print()
        print("  1. Quick Refresh")
        print("     - Uses existing cookies from config.json")
        print("     - Works only if your session is still active")
        print("     - Faster, no manual login needed")
        print()
        print("  2. Full Login  (recommended)")
        print("     - Opens a visible browser window")
        print("     - Log in manually to X/Twitter")
        print("     - Extracts all fresh parameters:")
        print("       cookies, x-client-transaction-id, query IDs")
        print("     - Use when quick refresh fails")
        print()

        try:
            choice = input("Choose (1/2, default 2): ").strip() or "2"
        except EOFError:
            choice = "2"

        try:
            cfg = self._load_config()
        except FileNotFoundError as exc:
            print(f"\nError: {exc}")
            print("Run setup_api_cookies.py first to create a config file.")
            sys.exit(1)

        if choice == "1":
            success = self.quick_refresh(cfg)
        elif choice == "2":
            success = self.full_login(cfg)
        else:
            print(f"\nInvalid choice: {choice}. Use 1 or 2.")
            sys.exit(1)

        if success:
            print("\nYou can now run your scripts:")
            print("  python3 historical_scripts/historical_runner.py")
            print("  python3 live_scripts/live_runner.py")
            print("  python3 search_scripts/search_runner.py --once")
        else:
            print(
                "\nRefresh failed. Ensure you have an active internet"
                " connection and valid X/Twitter credentials.\n"
            )
            sys.exit(1)


def main() -> None:
    """Entry point for the session updater."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    updater = SessionUpdater()
    updater.run()


if __name__ == "__main__":
    main()
