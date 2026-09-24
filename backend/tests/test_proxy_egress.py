"""One HTTPS proxy setting must reach Chromium without exposing its password."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import URLError

import pytest

from engine.browser import BrowserBootstrap, playwright_proxy, proxy_safe_error
from engine.auth import auto_refresh_session
from engine.observability import EventRecorder, redact_exception
from fetching.media import _store
from fetching.redaction import redact_text
from fetching.runner import _collect_run_summary


def test_proxy_reaches_browser_and_errors_do_not_leak_credentials(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://operator:verylongsecret@proxy.example:3128")
    assert playwright_proxy() == {
        "server": "http://proxy.example:3128",
        "username": "operator",
        "password": "verylongsecret",
    }
    launch = Mock()
    BrowserBootstrap._launch_chromium(SimpleNamespace(chromium=SimpleNamespace(launch=launch)), headless=True)
    assert launch.call_args.kwargs["proxy"] == playwright_proxy()

    failure = URLError("http://operator:verylongsecret@proxy.example:3128 failed")
    assert "verylongsecret" not in proxy_safe_error(failure)
    assert "verylongsecret" not in redact_text(str(failure))
    assert redact_text("operator finished a page") == "operator finished a page"
    monkeypatch.setenv("HTTPS_PROXY", "http://operator:tiny@proxy.example:3128")
    assert "tiny" not in redact_text("proxy password tiny failed")
    monkeypatch.setenv("HTTPS_PROXY", "http://operator:verylongsecret@proxy.example:3128")
    with patch("fetching.media.urlopen", side_effect=failure):
        assert _store("https://pbs.twimg.com/media/a.jpg", tmp_path) is None
    assert "verylongsecret" not in caplog.text


def test_auth_maintenance_uses_the_proxy_without_printing_it(monkeypatch, tmp_path, capsys, caplog):
    monkeypatch.setenv("HTTPS_PROXY", "http://operator:verylongsecret@proxy.example:3128")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"api_cookies": {"auth_token": "dummy-token"}}))
    playwright = Mock()
    playwright.chromium.launch.side_effect = RuntimeError("verylongsecret failed")
    context = Mock()
    context.__enter__ = Mock(return_value=playwright)
    context.__exit__ = Mock(return_value=False)
    with patch("engine.auth.sync_playwright", return_value=context):
        assert auto_refresh_session(config) is False
    assert playwright.chromium.launch.call_args_list[0].kwargs["proxy"] == playwright_proxy()
    assert "verylongsecret" not in capsys.readouterr().out + caplog.text


def test_proxy_failure_does_not_reach_persisted_run_artifacts(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTPS_PROXY", "http://operator:verylongsecret@proxy.example:3128")
    failure = URLError("http://operator:verylongsecret@proxy.example:3128 failed")
    recorder = EventRecorder(tmp_path / "data" / "search" / "logs", subsystem="search")
    recorder.emit("request_error", exception=redact_exception(failure))
    detail = recorder.emit_http_error(
        account="test", endpoint="SearchTimeline", status_code=407,
        cursor=None, request_url="https://x.com/i/api/graphql/abc/SearchTimeline",
        request_headers={}, variables={}, response_text=str(failure),
    )
    summary, ledger, _ = _collect_run_summary(tmp_path, "search", 1)
    assert "verylongsecret" not in json.dumps(summary) + json.dumps(ledger)
    assert "verylongsecret" not in recorder.events_file.read_text() + Path(detail).read_text()


@pytest.mark.parametrize("proxy", ["https://proxy.example:3128", "http://proxy.example:bad"])
def test_unsupported_proxy_fails_without_echoing_the_value(monkeypatch, proxy):
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    with pytest.raises(ValueError, match="HTTPS_PROXY must be an http:// proxy URL") as failure:
        playwright_proxy()
    assert proxy not in str(failure.value)
