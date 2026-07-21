"""Headful Playwright sniffer for X/Twitter network traffic.

Opens a real headful Chromium, logs the configured account in via cookies from
``config.json`` (or ``--config``), then streams every request/response the
browser makes to ``sniffer_runs/<utc-stamp>/requests.jsonl`` — one JSON object
per line. GraphQL requests are decoded into ``variables`` / ``features`` /
``fieldToggles`` and tagged with ``endpoint`` / ``query_id`` so the capture can
be diffed against what ``x_api/`` builds and sends.

You do the driving: once the window is open, navigate UserTweets /
UserTweetsAndReplies / SearchTimeline / UserByScreenName and scroll around.
Press Ctrl+C (or close the browser) to stop; ``manifest.json`` and the fresh
``cookies.json`` are written to the run dir on exit.

Run directly:

    python twitter_fetcher/diagnostics/sniffer.py [--config CONFIG]

Or as a module:

    python -m tweeter_data_fetcher.diagnostics.sniffer

Output files (under ``sniffer_runs/<utc-stamp>/``) are gitignored and contain
live auth tokens — keep them local.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

# Resolve everything from this file's location so the script runs standalone
# (``python sniffer.py``) without the package being importable.
DIAGNOSTICS = Path(__file__).resolve().parent
PROJECT_ROOT = DIAGNOSTICS.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"
RUNS_DIR = DIAGNOSTICS / "sniffer_runs"

HOME_URL = "https://x.com/home"
X_DOMAINS = ("x.com", "twitter.com")
GRAPHQL_PATH = "/i/api/graphql/"
_BODY_CAP_BYTES = 2_000_000  # cap a stored GraphQL response body (safety valve)
_LOGIN_HINTS = ("/login", "/i/flow/login")


def _utcnow_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _utcstamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _default_output_dir() -> Path:
    out = RUNS_DIR / _utcstamp()
    out.mkdir(parents=True, exist_ok=True)
    return out


def _load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _config_cookies(config: Dict[str, Any]) -> Dict[str, str]:
    raw = config.get("api_cookies") or {}
    cookies: Dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(value, str) or not value or value == "REPLACE_ME":
            continue
        cookies[name] = value
    return cookies


def _looks_logged_in(url: str) -> bool:
    return not any(hint in url for hint in _LOGIN_HINTS)


def _is_graphql(url: str) -> bool:
    return GRAPHQL_PATH in url


def _parse_graphql_url(url: str) -> Dict[str, Any]:
    """Pull endpoint / query_id / decoded variables / features / fieldToggles."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    # Expect: ... / i / api / graphql / <queryId> / <OperationName>
    out: Dict[str, Any] = {
        "endpoint": None,
        "query_id": None,
        "variables": None,
        "features": None,
        "fieldToggles": None,
    }
    if "graphql" in parts:
        idx = parts.index("graphql")
        if idx + 1 < len(parts):
            out["query_id"] = parts[idx + 1]
        if idx + 2 < len(parts):
            out["endpoint"] = parts[idx + 2]
    qs = parse_qs(parsed.query)
    for key in ("variables", "features", "fieldToggles"):
        values = qs.get(key)
        if not values:
            continue
        try:
            out[key] = json.loads(unquote(values[0]))
        except (ValueError, TypeError):
            out[key] = {"_raw": values[0]}
    return out


def _extract_cursors(node: Any) -> Dict[str, Optional[str]]:
    """Walk a GraphQL timeline payload for bottom/top cursor values."""
    found = {"bottom": None, "top": None}

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            ctype = obj.get("cursorType")
            entry = obj.get("entryId") or ""
            value = obj.get("value")
            if value and isinstance(ctype, str):
                if ctype.lower() == "bottom":
                    found["bottom"] = value
                elif ctype.lower() == "top":
                    found["top"] = value
            elif value and isinstance(entry, str):
                if entry.startswith("cursor-bottom-"):
                    found["bottom"] = value
                elif entry.startswith("cursor-top-"):
                    found["top"] = value
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(node)
    return found


def _truncate(text: Optional[str], limit: int) -> Optional[str]:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + f"\n…<+{len(text) - limit} bytes>"


class Recorder:
    """Append one JSONL record per request/response as it completes."""

    def __init__(self, jsonl_path: Path) -> None:
        self._fh = jsonl_path.open("w", encoding="utf-8")
        self.jsonl_path = jsonl_path
        self.seq = 0
        self.total = 0
        self.graphql = 0
        self.endpoints: Dict[str, int] = {}
        self.query_ids: Dict[str, int] = {}

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def on_response(self, response: Any) -> None:
        try:
            request = response.request
            url = request.url
            self.seq += 1
            self.total += 1
            record: Dict[str, Any] = {
                "seq": self.seq,
                "ts": _utcnow_iso(),
                "method": request.method,
                "url": url,
                "is_graphql": _is_graphql(url),
                "resource_type": getattr(request, "resource_type", None),
                "request_headers": dict(request.headers),
                "post_data": request.post_data,
                "referer": request.headers.get("referer"),
                "status": None,
                "ok": None,
                "response_headers": {},
            }

            try:
                record["status"] = response.status
                record["ok"] = response.ok
                record["response_headers"] = dict(response.headers)
            except Exception:
                pass

            if _is_graphql(url):
                self.graphql += 1
                parsed = _parse_graphql_url(url)
                endpoint = parsed["endpoint"]
                query_id = parsed["query_id"]
                if endpoint:
                    self.endpoints[endpoint] = self.endpoints.get(endpoint, 0) + 1
                if query_id:
                    self.query_ids[query_id] = self.query_ids.get(query_id, 0) + 1
                record.update(parsed)
                variables = parsed.get("variables") or {}
                record["cursor_in"] = variables.get("cursor") if isinstance(variables, dict) else None
                record["cursor_out"] = None
                record["response_body"] = None
                try:
                    body_bytes = response.body()
                    body_text = body_bytes.decode("utf-8", errors="replace")
                    record["response_body"] = _truncate(body_text, _BODY_CAP_BYTES)
                    try:
                        record["cursor_out"] = _extract_cursors(json.loads(body_text))
                    except (ValueError, TypeError):
                        pass
                except Exception as exc:  # body unavailable (cancelled / redirect)
                    record["response_body_error"] = str(exc)

            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()
            tag = f" {endpoint}:{record['status']}" if _is_graphql(url) and endpoint else ""
            print(f"[{self.seq:>5}] {record['method']:<5} {record['status']} {url[:100]}{tag}")
        except Exception as exc:  # never let a capture error kill the handler
            print(f"[sniffer] record error: {exc}", file=sys.stderr)

    def write_manifest(self, run_dir: Path, started_iso: str, ended_iso: str) -> Path:
        manifest = {
            "started_iso": started_iso,
            "ended_iso": ended_iso,
            "requests_total": self.total,
            "requests_graphql": self.graphql,
            "endpoints": self.endpoints,
            "query_ids": self.query_ids,
            "requests_jsonl": str(self.jsonl_path),
        }
        path = run_dir / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def write_cookies(self, run_dir: Path, context: Any) -> Path:
        """Dump the live cookie jar (x.com/twitter.com) so it can update config."""
        jar: Dict[str, str] = {}
        try:
            for cookie in context.cookies():
                domain = (cookie.get("domain") or "").lstrip(".")
                if not any(domain.endswith(d) for d in X_DOMAINS):
                    continue
                name = cookie.get("name")
                value = cookie.get("value")
                if name and value:
                    jar[name] = value
        except Exception as exc:
            print(f"[sniffer] cookie dump error: {exc}", file=sys.stderr)
        path = run_dir / "cookies.json"
        path.write_text(json.dumps(jar, indent=2, ensure_ascii=False), encoding="utf-8")
        return path


def capture(
    *,
    config_path: Path = CONFIG_PATH,
    output_dir: Optional[Path] = None,
    home_url: str = HOME_URL,
) -> Path:
    """Launch headful Chromium, capture X/Twitter traffic until Ctrl+C or close.

    Returns the run directory holding ``requests.jsonl`` / ``manifest.json`` /
    ``cookies.json``.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright is not installed. Run: pip install playwright && "
            "python -m playwright install chromium"
        ) from exc

    config = _load_config(config_path)
    cookies = _config_cookies(config)
    run_dir = output_dir or _default_output_dir()
    jsonl_path = run_dir / "requests.jsonl"
    recorder = Recorder(jsonl_path)

    started_iso = _utcnow_iso()
    print(f"[sniffer] run dir : {run_dir}")
    print(f"[sniffer] config  : {config_path}")
    print(f"[sniffer] cookies : {len(cookies)} from config "
          f"({'auth+ct0 present' if {'auth_token', 'ct0'} <= cookies.keys() else 'manual login needed'})")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        # Pre-seed the session so we land logged-in, exactly like x_api/client.py.
        if cookies:
            context.add_cookies(
                [{"name": k, "value": v, "domain": ".x.com", "path": "/"}
                 for k, v in cookies.items()]
            )
        page = context.new_page()
        context.on("response", recorder.on_response)

        page.goto(home_url, wait_until="domcontentloaded")
        if not _looks_logged_in(page.url):
            print("[sniffer] Browser landed on a login flow. Log in in the window, "
                  "then return here and press Enter.")
        else:
            print("[sniffer] Logged in via config cookies. If anything looks wrong, "
                  "log in manually in the window.")
        print("[sniffer] Press Enter to START capturing…")
        try:
            input()
        except EOFError:
            pass

        print("[sniffer] Capturing — navigate UserTweets / Replies / SearchTimeline / "
              "UserByScreenName and scroll. Ctrl+C or close the browser to stop.")
        try:
            while browser.is_connected():
                pages = [p for p in context.pages if not p.is_closed()]
                if not pages:
                    break
                pages[-1].wait_for_timeout(200)
        except KeyboardInterrupt:
            print("\n[sniffer] Stopping (Ctrl+C)…")
        finally:
            ended_iso = _utcnow_iso()
            try:
                recorder.write_manifest(run_dir, started_iso, ended_iso)
                recorder.write_cookies(run_dir, context)
            finally:
                recorder.close()
            print(f"[sniffer] wrote {recorder.total} requests "
                  f"({recorder.graphql} graphql) → {jsonl_path}")
            print(f"[sniffer] manifest + cookies → {run_dir}")
            try:
                context.close()
                browser.close()
            except Exception:
                pass

    return run_dir


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Headful X/Twitter request sniffer.")
    parser.add_argument(
        "--config", type=Path, default=CONFIG_PATH,
        help=f"Config JSON with api_cookies (default: {CONFIG_PATH})",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Run output dir (default: sniffer_runs/<utc-stamp>)",
    )
    parser.add_argument(
        "--home", type=str, default=HOME_URL,
        help=f"Starting URL (default: {HOME_URL})",
    )
    args = parser.parse_args(argv)
    capture(config_path=args.config, output_dir=args.output, home_url=args.home)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
