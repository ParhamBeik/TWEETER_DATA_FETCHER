#!/usr/bin/env python3
"""
Diagnostic GraphQL sniffer for X/Twitter (headful Selenium).

This is a *pure diagnostic* tool: it opens a headful Chrome window (driven by
plain Selenium), navigates to a target (account page or URL), and records every
Twitter GraphQL request/response pair **in arrival order** — the request headers
and bodies the page's own JS sends, plus the full response bodies — into the run
directory:

  - ``timeline.jsonl``   (machine-readable, one JSON object per line)
  - ``timeline.html``    (human-readable waterfall with collapsible bodies)
  - ``contract.json``    (structured per-endpoint request contract)
  - ``playbook.md``      (paste-ready markdown for AGENTS.md / README.md)

The playbook is the "template you can update": it summarises the live request
shape (query-ids, variables, features, fieldToggles) and documents the
``x-client-transaction-id`` algorithm so you can keep the request contract
current without guessing.

**How it captures:** before any page script runs, a small fetch/XHR interceptor
is injected via the Chrome DevTools Protocol
(``Page.addScriptToEvaluateOnNewDocument``). The interceptor hooks the page's
own ``window.fetch`` and ``XMLHttpRequest`` so it records every GraphQL call
*exactly as the page issues it* — including the JS-generated headers such as
``x-client-transaction-id``, ``x-csrf-token`` and ``authorization``. Python then
drains the captured pairs each second.

It does NOT drive the fetcher and does NOT write ``config.json``. To bake
captured query-ids / transaction-ids into config, use
``shared/auth/session_updater.py`` (it owns the apply step).

Run it directly:

    python -m shared.auth.graphql_sniffer elonmusk --timeout 90
    python -m shared.auth.graphql_sniffer https://x.com/elonmusk/with_replies

Dependencies (only needed to actually capture — ``--help`` works without them):

    pip3 install selenium
    # Chrome + a driver are also required; modern Selenium ships selenium-manager
    # which fetches a matching chromedriver automatically. (selenium-wire is NOT
    # needed and is deliberately avoided — it is fragile with modern ``blinker``.)

Note: the JS interceptor sees only headers the page sets explicitly (the
X-specific dynamic headers we care about). Browser-default headers such as
``user-agent`` / ``sec-ch-ua`` are added by the browser, not the page, so they
are not captured here — they are constant per profile anyway.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

try:
    import jdatetime
except ImportError:  # pragma: no cover - optional dependency
    jdatetime = None

try:
    import pytz
except ImportError:  # pragma: no cover - optional dependency
    pytz = None


# Endpoint labels (for readability; TweetDetail added for completeness).
ENDPOINT_CONFIG_KEYS = {
    "UserByScreenName": "user_by_screen_name_query_id",
    "UserTweets": "user_tweets_query_id",
    "UserTweetsAndReplies": "user_tweets_and_replies_query_id",
    "SearchTimeline": "search_timeline_query_id",
    "TweetDetail": "tweet_detail_query_id",
}

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "config.json"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEHRAN_TZ = pytz.timezone("Asia/Tehran") if pytz else None

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Annotations shown in the playbook for notable request headers. Anything not
# listed here is reported as plain "static" in the contract.
HEADER_NOTES = {
    "authorization": "public web Bearer (constant; same for all clients)",
    "x-twitter-auth-type": "OAuth2Session when authenticated (constant per session)",
    "x-csrf-token": "session-bound; equals the ct0 cookie value",
    "x-client-transaction-id": "dynamic — rotates per request (see playbook algorithm)",
    "referer": "page-dependent",
    "x-twitter-active-user": "yes (constant)",
    "x-twitter-client-language": "en (constant)",
}

# In-page fetch/XHR interceptor. Installed before any page script runs via CDP
# ``Page.addScriptToEvaluateOnNewDocument``. Records GraphQL request/response
# pairs onto ``window.__sniffer__.events``, each tagged with a request ``rid``
# so Python can pair request <-> response.
_INTERCEPT_JS = r"""
(function () {
  if (window.__sniffer__) return;
  var G = (window.__sniffer__ = { events: [], seq: 0 });
  function isQL(u) { return !!u && (/\/i\/api\/graphql\//.test(u) || /\/api\/graphql\//.test(u)); }
  function headersFrom(h) {
    var out = {};
    try {
      if (!h) return out;
      if (typeof h.forEach === 'function') h.forEach(function (v, k) { out[k] = v; });
      else for (var k in h) if (Object.prototype.hasOwnProperty.call(h, k)) out[k] = h[k];
    } catch (e) {}
    return out;
  }
  /* ---- fetch ---- */
  var origFetch = window.fetch;
  if (origFetch && !origFetch.__sniff) {
    var wf = function (input, init) {
      try {
        var url = typeof input === 'string' ? input : ((input && input.url) || '');
        if (isQL(url)) {
          var method = (init && init.method) || (input && input.method) || 'GET';
          var headers = headersFrom((init && init.headers) || (input && input.headers));
          var body = (init && init.body) ? String(init.body) : null;
          var rid = ++G.seq;
          G.events.push({ rid: rid, phase: 'request', url: url, method: method, headers: headers, body: body, ts: Date.now() });
          var p = origFetch.apply(this, arguments);
          p.then(function (resp) {
            try {
              var rh = {};
              resp.headers.forEach(function (v, k) { rh[k] = v; });
              resp.clone().text().then(function (t) {
                G.events.push({ rid: rid, phase: 'response', url: url, method: method, status: resp.status, headers: rh, body: t, ts: Date.now() });
              }).catch(function () {
                G.events.push({ rid: rid, phase: 'response', url: url, method: method, status: resp.status, headers: rh, body: null, ts: Date.now() });
              });
            } catch (e) {}
          }).catch(function () {});
          return p;
        }
      } catch (e) {}
      return origFetch.apply(this, arguments);
    };
    wf.__sniff = true;
    window.fetch = wf;
  }
  /* ---- XMLHttpRequest ---- */
  if (window.XMLHttpRequest) {
    var Open = XMLHttpRequest.prototype.open, Send = XMLHttpRequest.prototype.send, SetH = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.open = function (m, u) { this.__s_m = m; this.__s_u = u; this.__s_h = {}; return Open.apply(this, arguments); };
    XMLHttpRequest.prototype.setRequestHeader = function (k, v) { try { (this.__s_h = this.__s_h || {})[k] = v; } catch (e) {} return SetH.apply(this, arguments); };
    XMLHttpRequest.prototype.send = function (body) {
      var url = this.__s_u, method = this.__s_m;
      if (isQL(url)) {
        var rid = ++G.seq;
        var xhr = this;
        G.events.push({ rid: rid, phase: 'request', url: url, method: method || 'GET', headers: this.__s_h || {}, body: body ? String(body) : null, ts: Date.now() });
        this.addEventListener('loadend', function () {
          try {
            var rh = {};
            xhr.getAllResponseHeaders().trim().split(/\r?\n/).forEach(function (l) { var i = l.indexOf(': '); if (i > 0) rh[l.slice(0, i).toLowerCase()] = l.slice(i + 2); });
            G.events.push({ rid: rid, phase: 'response', url: url, method: method || 'GET', status: xhr.status, headers: rh, body: xhr.responseText, ts: Date.now() });
          } catch (e) {}
        });
      }
      return Send.apply(this, arguments);
    };
  }
})();
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _load_config(config_path: Path) -> Dict[str, Any]:
    with Path(config_path).open("r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload if isinstance(payload, dict) else {}


def _endpoint_from_url(url: str) -> Optional[Tuple[str, str]]:
    """Return ``(endpoint, query_id)`` from a GraphQL URL, or ``None``."""
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "graphql" not in parts:
        return None
    index = parts.index("graphql")
    if len(parts) <= index + 2:
        return None
    return parts[index + 2], parts[index + 1]


def _parse_query_params(url: str) -> Dict[str, Any]:
    """Parse the GraphQL GET query string (variables/features/fieldToggles)."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    out: Dict[str, Any] = {}
    for key in ("variables", "features", "fieldToggles"):
        raw = qs.get(key, [None])[0]
        if not raw:
            continue
        try:
            out[key] = json.loads(unquote(raw))
        except Exception:
            out[key] = unquote(raw)
    return out


def _is_graphql(url: str) -> bool:
    return "/i/api/graphql/" in url or "/api/graphql/" in url


def _jalali_batch_name() -> str:
    if TEHRAN_TZ is not None:
        now = jdatetime.datetime.now(TEHRAN_TZ) if jdatetime else None
        if now is not None:
            return now.strftime("%Y-%m-%d_%H-%M")
    # Fallback: plain UTC stamp (no tz/jdatetime available).
    import datetime as _dt
    return _dt.datetime.utcnow().strftime("%Y-%m-%d_%H-%M")


def _default_output_dir() -> Path:
    out = PROJECT_ROOT / "sniffer_runs" / _jalali_batch_name()
    out.mkdir(parents=True, exist_ok=True)
    return out


def _resolve_target(target: str) -> str:
    """Accept an account name or a full URL and return a navigation URL."""
    value = str(target or "").strip()
    if not value:
        return "https://x.com/home"
    if value.startswith("http://") or value.startswith("https://"):
        return value
    username = value.lstrip("@")
    return f"https://x.com/{username}"


def _parse_body_text(text: Any) -> Any:
    """Best-effort decode a captured body string as JSON, else return the text."""
    if text is None or text == "":
        return None
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except Exception:
        return text


# --------------------------------------------------------------------------- #
# Capture (headful Selenium + in-page fetch/XHR interceptor)
# --------------------------------------------------------------------------- #

def _install_interceptor(driver: Any) -> None:
    """Enable Page/Network and inject the interceptor before any page script."""
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Page.enable", {})
    except Exception:
        pass
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument", {"source": _INTERCEPT_JS}
    )


def _poll_interceptor(
    driver: Any,
    events: List[Dict[str, Any]],
    rid_to_seq: Dict[int, int],
    next_seq: Any,
) -> None:
    """Drain captured pairs from the page into ``events`` (arrival order)."""
    try:
        raw = driver.execute_script(
            "return (window.__sniffer__ && window.__sniffer__.events.splice(0, 500)) || [];"
        )
    except Exception:
        return
    if not raw:
        return
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or ""
        if not _is_graphql(url):
            continue
        parsed = _endpoint_from_url(url)
        endpoint, query_id = parsed if parsed else ("UnknownEndpoint", "UnknownQueryID")
        phase = item.get("phase")
        rid = item.get("rid")
        if phase == "request":
            seq = next_seq()
            if rid is not None:
                rid_to_seq[rid] = seq
            params = _parse_query_params(url)
            headers = {str(k): str(v) for k, v in (item.get("headers") or {}).items()}
            events.append({
                "seq": seq,
                "phase": "request",
                "timestamp_ms": int(item.get("ts") or time.time() * 1000),
                "endpoint": endpoint,
                "query_id": query_id,
                "method": item.get("method") or "GET",
                "url": unquote(url),
                "variables": params.get("variables"),
                "features": params.get("features"),
                "fieldToggles": params.get("fieldToggles"),
                "headers": headers,
                "post_data": _parse_body_text(item.get("body")),
                "referer": headers.get("referer") or headers.get("Referer"),
            })
        elif phase == "response":
            headers = {str(k): str(v) for k, v in (item.get("headers") or {}).items()}
            events.append({
                "seq": next_seq(),
                "phase": "response",
                "timestamp_ms": int(item.get("ts") or time.time() * 1000),
                "endpoint": endpoint,
                "query_id": query_id,
                "url": unquote(url),
                "status": item.get("status"),
                "headers": headers,
                "body": _parse_body_text(item.get("body")),
                "request_seq": rid_to_seq.get(rid) if rid is not None else None,
            })


def observe(
    target: str,
    *,
    timeout_seconds: int = 120,
    output_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
) -> Path:
    """
    Open a headful Chrome (Selenium), navigate to ``target`` (account name or
    URL), and record every GraphQL request/response pair in arrival order.

    Returns the output directory containing ``timeline.jsonl``, ``timeline.html``,
    ``contract.json`` and ``playbook.md``.
    """
    try:
        from selenium import webdriver  # type: ignore
        from selenium.webdriver.chrome.options import Options  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "selenium is required to capture. Install with:\n"
            "  pip3 install selenium\n"
            "Chrome + a driver are also needed; modern Selenium ships "
            "selenium-manager, which fetches a matching chromedriver."
        ) from exc

    config_path = Path(config_path or CONFIG_PATH)
    output_dir = Path(output_dir) if output_dir else _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "timeline.jsonl"
    html_path = output_dir / "timeline.html"
    contract_path = output_dir / "contract.json"
    playbook_path = output_dir / "playbook.md"

    config = _load_config(config_path)
    cookies = config.get("api_cookies", {}) or {}
    user_agent = (
        config.get("api_headers", {}).get("user-agent") or DEFAULT_USER_AGENT
    )
    nav_url = _resolve_target(target)

    options = Options()
    options.add_argument(f"--user-agent={user_agent}")
    options.add_argument("--window-size=1280,800")
    # Headful: intentionally no --headless.

    driver = webdriver.Chrome(options=options)

    events: List[Dict[str, Any]] = []
    rid_to_seq: Dict[int, int] = {}
    seq_counter = [0]

    def _next_seq() -> int:
        seq_counter[0] += 1
        return seq_counter[0]

    try:
        # Inject the interceptor before any page script runs, so it survives
        # every navigation (including the cookie-setup navigation below).
        _install_interceptor(driver)

        # Selenium can only set cookies once we are on the target domain.
        try:
            driver.get("https://x.com")
        except Exception as exc:
            print(f"[!] Initial navigation warning: {exc}")
        for name, value in cookies.items():
            if not value:
                continue
            try:
                driver.add_cookie({
                    "name": str(name),
                    "value": str(value),
                    "domain": ".x.com",
                    "path": "/",
                    "secure": True,
                })
            except Exception:
                pass

        print(f"[*] Sniffer output dir: {output_dir}")
        print(f"[*] Navigating to {nav_url}")
        print("[*] The browser will close automatically after the timeout,")
        print("    or close the window yourself to stop early.")
        try:
            driver.get(nav_url)
        except Exception as exc:
            print(f"[!] Navigation warning: {exc}")

        deadline = time.time() + max(10, int(timeout_seconds))
        while time.time() < deadline:
            _poll_interceptor(driver, events, rid_to_seq, _next_seq)
            # Stop early if the user closed the window.
            try:
                _ = driver.window_handles
            except Exception:
                break
            time.sleep(1)
    finally:
        try:
            _poll_interceptor(driver, events, rid_to_seq, _next_seq)
        except Exception:
            pass
        try:
            driver.quit()
        except Exception:
            pass

    _write_jsonl(jsonl_path, events)
    _write_html(html_path, events)
    contract = _extract_contract(events)
    _write_contract_json(contract_path, contract)
    _write_playbook(playbook_path, contract, events, output_dir.name)

    n_req = sum(1 for e in events if e["phase"] == "request")
    n_res = sum(1 for e in events if e["phase"] == "response")
    print(f"[+] Captured {len(events)} events ({n_req} requests, {n_res} responses).")
    print(f"[+] Wrote {jsonl_path}")
    print(f"[+] Wrote {html_path}")
    print(f"[+] Wrote {contract_path}")
    print(f"[+] Wrote {playbook_path}")
    return output_dir


# --------------------------------------------------------------------------- #
# Contract + playbook (the "template you can update")
# --------------------------------------------------------------------------- #

def _extract_contract(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Build a structured per-endpoint contract from the captured events."""
    resp_by_req: Dict[Any, Dict[str, Any]] = {}
    for e in events:
        if e.get("phase") == "response" and e.get("request_seq") is not None:
            resp_by_req[e["request_seq"]] = e

    contract: Dict[str, Dict[str, Any]] = {}
    for e in events:
        if e.get("phase") != "request":
            continue
        ep = e.get("endpoint") or "UnknownEndpoint"
        entry = contract.setdefault(ep, {
            "endpoint": ep,
            "query_id": e.get("query_id"),
            "method": e.get("method"),
            "url_template": f"https://x.com/i/api/graphql/{e.get('query_id')}/{ep}",
            "variables_sample": e.get("variables"),
            "features": e.get("features"),
            "fieldToggles": e.get("fieldToggles"),
            "request_count": 0,
            "request_headers_seen": [],
            "header_notes": {},
            "referer_sample": None,
            "response_status_codes": [],
            "sample_rate_limit": None,
        })
        entry["request_count"] += 1
        headers = e.get("headers") or {}
        for key in headers:
            kl = key.lower()
            if kl not in entry["request_headers_seen"]:
                entry["request_headers_seen"].append(kl)
        ref = headers.get("referer") or headers.get("Referer")
        if ref and not entry["referer_sample"]:
            entry["referer_sample"] = ref
        resp = resp_by_req.get(e.get("seq"))
        if resp:
            status = resp.get("status")
            if status is not None:
                entry["response_status_codes"].append(status)
            rh = resp.get("headers") or {}
            if rh.get("x-rate-limit-limit") and not entry["sample_rate_limit"]:
                entry["sample_rate_limit"] = {
                    "limit": rh.get("x-rate-limit-limit"),
                    "remaining": rh.get("x-rate-limit-remaining"),
                    "reset": rh.get("x-rate-limit-reset"),
                }

    for entry in contract.values():
        entry["header_notes"] = {
            h: HEADER_NOTES.get(h, "static") for h in entry["request_headers_seen"]
        }
    return contract


def _write_contract_json(path: Path, contract: Dict[str, Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(contract, f, ensure_ascii=False, indent=2)


def _compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ": "))


def _write_playbook(
    path: Path,
    contract: Dict[str, Dict[str, Any]],
    events: List[Dict[str, Any]],
    batch: str,
) -> None:
    """Write a paste-ready markdown playbook for AGENTS.md / README.md."""
    n_req = sum(1 for e in events if e["phase"] == "request")
    n_res = sum(1 for e in events if e["phase"] == "response")

    lines: List[str] = []
    lines.append(f"# GraphQL Request Playbook — {batch}")
    lines.append("")
    lines.append(
        "Generated by the headful Selenium GraphQL sniffer from a live capture. "
        "Paste the relevant tables into `AGENTS.md` / `README.md` to keep the "
        "request contract current instead of guessing."
    )
    lines.append("")
    lines.append(
        "> The run directory also contains `timeline.jsonl` / `timeline.html` "
        "with **full headers and response bodies**, including sensitive tokens "
        "(authorization, ct0/csrf). It is gitignored — do not commit it."
    )
    lines.append("")
    lines.append(
        f"Capture summary: **{n_req}** GraphQL requests, **{n_res}** responses "
        f"across **{len(contract)}** endpoint(s)."
    )
    lines.append("")

    if contract:
        lines.append("## Endpoints")
        lines.append("")
        lines.append("| Endpoint | Query ID | Method | fieldToggles | Referer (sample) |")
        lines.append("|---|---|---|---|---|")
        for entry in contract.values():
            ft = entry.get("fieldToggles")
            ft_str = _compact(ft) if ft else "—"
            ref = entry.get("referer_sample") or "—"
            lines.append(
                f"| `{entry.get('endpoint')}` | `{entry.get('query_id')}` | "
                f"{entry.get('method')} | `{escape(ft_str)}` | {escape(ref)} |"
            )
        lines.append("")

    lines.append("## Per-endpoint detail")
    lines.append("")
    for entry in contract.values():
        lines.append(f"### `{entry.get('endpoint')}`")
        lines.append("")
        lines.append(f"- **URL:** `{entry.get('url_template')}`")
        lines.append(f"- **Method:** {entry.get('method')}")
        statuses = entry.get("response_status_codes") or []
        if statuses:
            from collections import Counter
            counts = Counter(statuses)
            lines.append(
                "- **Response status codes seen:** "
                + ", ".join(f"{code} ×{n}" for code, n in sorted(counts.items()))
            )
        rl = entry.get("sample_rate_limit")
        if rl:
            lines.append(
                f"- **Sample rate limit:** {rl.get('limit')} limit, "
                f"{rl.get('remaining')} remaining, resets {rl.get('reset')}"
            )
        variables = entry.get("variables_sample")
        if variables is not None:
            lines.append("- **Variables (sample):**")
            lines.append("```json")
            lines.append(json.dumps(variables, ensure_ascii=False, indent=2))
            lines.append("```")
        features = entry.get("features")
        if isinstance(features, dict):
            lines.append(
                f"- **Features:** {len(features)} flags "
                f"(incl. `post_ctas_fetch_enabled: "
                f"{features.get('post_ctas_fetch_enabled')}`) — see `contract.json`"
            )
        lines.append("")

    lines.append("## Request headers")
    lines.append("")
    lines.append("Captured browser request headers split into two groups:")
    lines.append("")
    lines.append("**Static / session-bound** (constant across requests):")
    lines.append(
        "- `authorization` — the public web Bearer (constant; identical for all clients)."
    )
    lines.append("- `x-twitter-auth-type: OAuth2Session` — present when authenticated.")
    lines.append("- `x-csrf-token` — session-bound; equals the `ct0` cookie value.")
    lines.append(
        "- `x-twitter-client-language`, `x-twitter-active-user`, `content-type`,"
        " `user-agent`, `sec-ch-ua*`, `accept*` — constant per browser profile."
    )
    lines.append("")
    lines.append("**Dynamic / per-request**:")
    lines.append("- `x-client-transaction-id` — rotates every request (see below).")
    lines.append("- `referer` — depends on the page that triggered the call.")
    lines.append("")

    lines.append("## `x-client-transaction-id` algorithm (reference)")
    lines.append("")
    lines.append(
        "`x-client-transaction-id` is a per-request header generated **client-side** "
        "by X's web bundle. Known, publicly reverse-engineered facts:"
    )
    lines.append("")
    lines.append(
        "- It is derived from the request's HTTP **method + path** plus an "
        "**animation-frame / timing seed** (the so-called color/animation-frame "
        "hash), producing a long alphanumeric string whose leading bytes encode "
        "the method and path."
    )
    lines.append("- It **rotates on every request** — it is not a session token.")
    lines.append(
        "- It is **not strictly validated** server-side: requests frequently "
        "succeed with an empty or random value. The project exploits this — "
        "`api_headers.x-client-transaction-id` is left blank and `APIManager` "
        "generates a fallback session transaction id at runtime."
    )
    lines.append(
        "- Reproducing it exactly requires porting the obfuscated generator out "
        "of the main web bundle — out of scope for this tool; the playbook only "
        "**documents** the behaviour."
    )
    lines.append("")

    lines.append("## Keeping the contract current")
    lines.append("")
    lines.append("1. Capture a fresh run:")
    lines.append("   ```")
    lines.append("   python -m shared.auth.graphql_sniffer <target> --timeout 90")
    lines.append("   ```")
    lines.append(
        "2. Copy the **Endpoints** table above into the "
        '"Sniffer-Derived GraphQL Request Contract" section of `AGENTS.md`.'
    )
    lines.append(
        "3. To bake captured query-ids / transaction-id into `config.json`, run "
        "`shared/auth/session_updater.py` — the sniffer itself never writes config."
    )
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Output writers
# --------------------------------------------------------------------------- #

def _write_jsonl(path: Path, events: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _write_html(path: Path, events: List[Dict[str, Any]]) -> None:
    """Self-contained waterfall HTML (no external deps)."""
    rows_html: List[str] = []
    for event in events:
        phase = event.get("phase")
        seq = event.get("seq")
        endpoint = event.get("endpoint") or "—"
        query_id = event.get("query_id") or "—"
        method = event.get("method") or "—"
        status = event.get("status")
        status_str = str(status) if status is not None else "—"
        ts = event.get("timestamp_ms")

        req_headers = event.get("headers") if phase == "request" else {}
        if phase == "response":
            parent = next((e for e in events if e.get("seq") == event.get("request_seq")), None)
            req_headers = parent.get("headers", {}) if parent else {}

        client_tx = req_headers.get("x-client-transaction-id", "—")
        csrf = req_headers.get("x-csrf-token", "—")
        auth_type = req_headers.get("x-twitter-auth-type", "—")

        resp_headers = event.get("headers") if phase == "response" else {}
        server_tx = resp_headers.get("x-transaction-id", "—")
        rate_remaining = resp_headers.get("x-rate-limit-remaining", "—")
        rate_reset = resp_headers.get("x-rate-limit-reset", "—")
        resp_time = resp_headers.get("x-response-time", "—")

        phase_label = "REQ" if phase == "request" else f"RES {status_str}"
        phase_class = "req" if phase == "request" else ("res-ok" if (status or 0) < 400 else "res-err")

        body = event.get("body") if phase == "response" else event.get("post_data")
        body_text = "" if body is None else (json.dumps(body, ensure_ascii=False, indent=2) if not isinstance(body, str) else body)
        body_preview = escape(body_text[:5000])
        has_body = bool(body_text)

        rows_html.append(f"""
        <tr class="{phase_class}">
          <td>{seq}</td>
          <td>{ts}</td>
          <td>{escape(phase_label)}</td>
          <td>{escape(str(endpoint))}</td>
          <td><code>{escape(str(query_id))}</code></td>
          <td>{escape(str(method))}</td>
          <td>{escape(str(client_tx))}</td>
          <td>{escape(str(csrf))[:24]}</td>
          <td>{escape(str(auth_type))}</td>
          <td>{escape(str(server_tx))}</td>
          <td>{escape(str(rate_remaining))}/{escape(str(rate_reset))}</td>
          <td>{escape(str(resp_time))}</td>
          <td>{'<details><summary>show</summary><pre>' + body_preview + '</pre></details>' if has_body else '—'}</td>
        </tr>""")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>GraphQL Sniffer Timeline</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, sans-serif; margin: 16px; background: #f7f7f8; color: #222; }}
  h1 {{ font-size: 18px; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff; font-size: 12px; }}
  th, td {{ border: 1px solid #ddd; padding: 4px 6px; vertical-align: top; text-align: left; }}
  th {{ background: #eee; position: sticky; top: 0; }}
  tr.req {{ background: #eef6ff; }}
  tr.res-ok {{ background: #eefbef; }}
  tr.res-err {{ background: #fdecea; }}
  code {{ font-family: ui-monospace, Menlo, monospace; font-size: 11px; }}
  pre {{ white-space: pre-wrap; word-break: break-word; max-height: 400px; overflow: auto; font-size: 11px; }}
  details summary {{ cursor: pointer; }}
</style>
</head>
<body>
<h1>GraphQL Sniffer Timeline — {escape(str(path.parent.name))}</h1>
<p>{len(events)} events captured. Headers shown per paired request/response.</p>
<table>
  <thead><tr>
    <th>seq</th><th>ts(ms)</th><th>phase</th><th>endpoint</th><th>query_id</th><th>method</th>
    <th>x-client-transaction-id</th><th>x-csrf-token</th><th>x-twitter-auth-type</th>
    <th>x-transaction-id (resp)</th><th>rate-limit rem/reset</th><th>x-response-time</th>
    <th>body</th>
  </tr></thead>
  <tbody>
{''.join(rows_html)}
  </tbody>
</table>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diagnostic X/Twitter GraphQL sniffer (headful Selenium).",
    )
    parser.add_argument(
        "target",
        help="Account name (e.g. elonmusk) or full https://x.com/... URL.",
    )
    parser.add_argument(
        "--timeout", type=int, default=120,
        help="Seconds to keep the browser open and capture (default: 120).",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Output directory for timeline.jsonl/.html, contract.json, playbook.md "
             "(default: sniffer_runs/<jalali_batch>/).",
    )
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir) if args.output_dir else None
    observe(args.target, timeout_seconds=args.timeout, output_dir=output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
