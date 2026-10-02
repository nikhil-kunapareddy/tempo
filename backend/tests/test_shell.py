"""Tests for the desktop shell's decision logic.

`opens_in_window` is the rule that used to live in main.rs's `on_navigation` callback, and it
is the reason Google sign-in works at all: Google rejects OAuth in an embedded webview, so the
consent screen has to be pushed out to the system browser. Getting it wrong in either
direction is bad — too strict and the app itself opens in Safari, too loose and sign-in breaks
with `disallowed_useragent`.

Imported through a Cocoa-free path: the module-level `import AppKit` in shell.py means these
tests only run where pyobjc is installed, which on this Apple-Silicon-only project is fine.
"""

from __future__ import annotations

import pytest

from backend.app import config

shell = pytest.importorskip("desktop.shell", reason="pyobjc not installed")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/",
        "http://localhost:8317/api/chat",
        "http://127.0.0.1:8000/",
        "http://LocalHost:8000/settings",  # host comparison is case-insensitive
        "https://localhost:8000/",
        "about:blank",  # the splash, injected with loadHTMLString:
    ],
)
def test_stays_in_window(url):
    assert shell.opens_in_window(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://accounts.google.com/o/oauth2/v2/auth?client_id=x",  # the whole point
        "https://example.com/",
        "http://localhost.evil.com/",  # suffix, not our host
        "http://notlocalhost/",
        "https://www.googleapis.com/calendar/v3",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "",
        None,
    ],
)
def test_goes_to_browser(url):
    assert shell.opens_in_window(url) is False


def test_every_candidate_port_stays_in_window():
    """The port is chosen at launch, so all of them must be treated as same-origin."""
    for port in config.CANDIDATE_PORTS:
        assert shell.opens_in_window(f"http://localhost:{port}/") is True


class TestPortSelection:
    def test_prefers_the_first_free_candidate(self, monkeypatch):
        taken = {config.CANDIDATE_PORTS[0]}
        monkeypatch.setattr(config, "port_is_free", lambda host, port: port not in taken)
        assert config.pick_free_port("127.0.0.1") == config.CANDIDATE_PORTS[1]

    def test_none_when_all_taken(self, monkeypatch):
        monkeypatch.setattr(config, "port_is_free", lambda host, port: False)
        assert config.pick_free_port("127.0.0.1") is None

    def test_reports_a_bound_port_as_busy(self):
        """Guards the real socket probe, not a stub of it."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            assert config.port_is_free("127.0.0.1", port) is False
        assert config.port_is_free("127.0.0.1", port) is True


def test_file_inputs_get_a_picker():
    """WKWebView shows nothing for <input type="file"> unless the UI delegate runs a panel.

    The method also has to carry its explicit block signature: if pyobjc hands WebKit's
    completion handler over unsigned, calling it raises inside a WebKit callback and the app
    dies the moment someone picks a file.
    """
    delegate = shell.TempoWebDelegate.alloc().initWithOnQuit_(lambda: None)
    selector = b"webView:runOpenPanelWithParameters:initiatedByFrame:completionHandler:"
    assert delegate.respondsToSelector_(selector)
    method = delegate.methodForSelector_(selector)
    assert method is not None
    assert shell._OPEN_PANEL_SIGNATURE == b"v@:@@@@?<v@?@>"
