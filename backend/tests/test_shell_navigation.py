"""End-to-end check that the shell's navigation delegate is wired to WebKit correctly.

`test_shell.py` covers the *rule*; this covers the *plumbing*, which is the part that actually
broke while porting the shell off Rust. The decision handler WebKit passes in is an Objective-C
block, and pyobjc will happily accept a delegate method whose block argument has no declared
signature — then raise TypeError from inside the WebKit callback, which becomes an uncaught
ObjC exception and kills the app on the first navigation. Nothing short of driving a real
WKWebView catches that, so this drives one.

Needs a WindowServer connection (WebKit spawns helper processes), so it skips rather than fails
where there isn't one.
"""

from __future__ import annotations

import pytest

pytest.importorskip("WebKit", reason="pyobjc not installed")

import time  # noqa: E402

from AppKit import NSApplication, NSApplicationActivationPolicyAccessory  # noqa: E402
from Foundation import NSDate, NSDefaultRunLoopMode, NSMakeRect, NSRunLoop  # noqa: E402
from WebKit import WKWebView, WKWebViewConfiguration  # noqa: E402

from desktop import shell  # noqa: E402

TIMEOUT = 15.0
EXTERNAL_URL = "https://accounts.google.com/o/oauth2/v2/auth?client_id=test"


@pytest.fixture
def webview():
    """An off-screen WKWebView. No window is needed to exercise the navigation delegate."""
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
    )
    try:
        view = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, 400, 300), WKWebViewConfiguration.alloc().init()
        )
    except Exception as exc:  # pragma: no cover - only on a session without a WindowServer
        pytest.skip(f"cannot create a WKWebView here: {exc}")
    if view is None:  # pragma: no cover
        pytest.skip("cannot create a WKWebView here")
    return view


def _run_until(predicate) -> None:
    """Pump the run loop until `predicate` holds or TIMEOUT elapses.

    Deliberately not PyObjCTools.AppHelper: its runEventLoop/stopEventLoop pair drives NSApp
    and tears it down on stop, which exits the interpreter mid-test — pytest dies without
    reporting, silently truncating the rest of the suite. WebKit only needs the run loop
    pumped, so pump it directly and leave NSApp alone.
    """
    deadline = time.monotonic() + TIMEOUT
    loop = NSRunLoop.currentRunLoop()
    while time.monotonic() < deadline and not predicate():
        loop.runMode_beforeDate_(
            NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.02)
        )


def test_offsite_navigation_is_handed_to_the_browser(webview, monkeypatch):
    """The Google consent screen must leave the app: Google blocks OAuth in embedded webviews.

    Also the regression test for the block signature — if the decision handler can't be called,
    this dies here instead of in front of a user.
    """
    opened: list[str] = []
    monkeypatch.setattr(shell, "open_externally", opened.append)

    delegate = shell.TempoWebDelegate.alloc().initWithOnQuit_(lambda: None)
    webview.setNavigationDelegate_(delegate)
    webview.setUIDelegate_(delegate)

    # Same-tab, script-driven navigation: exactly how the UI starts Google sign-in.
    webview.loadHTMLString_baseURL_(
        f'<script>window.location.href = "{EXTERNAL_URL}";</script>', None
    )
    _run_until(lambda: bool(opened))

    assert opened, "off-site navigation never reached open_externally"
    assert opened[0].startswith("https://accounts.google.com/")
    # Cancelled, so the webview itself must not have gone there.
    current = webview.URL()
    assert current is None or "accounts.google.com" not in current.absoluteString()


def test_local_navigation_is_allowed(webview, monkeypatch):
    """The splash loads as about:blank and must stay put rather than opening in Safari."""
    opened: list[str] = []
    monkeypatch.setattr(shell, "open_externally", opened.append)

    delegate = shell.TempoWebDelegate.alloc().initWithOnQuit_(lambda: None)
    webview.setNavigationDelegate_(delegate)

    webview.loadHTMLString_baseURL_("<h1>splash</h1>", None)
    _run_until(lambda: not webview.isLoading())

    assert opened == []
