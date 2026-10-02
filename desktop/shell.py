"""Tempo's macOS desktop shell: an NSWindow wrapping a WKWebView, driven from Python.

This is the Cocoa half of the shell. It knows nothing about the server beyond a base URL —
`main.py` owns the server's lifecycle and tells the shell when to swap the splash for the
real UI.

Two things here are load-bearing rather than incidental:

  - **Navigation policy.** Google returns `disallowed_useragent` for OAuth inside an embedded
    webview, so the consent screen *must* leave the app. `webView:decidePolicyForNavigation
    Action:` sends anything that isn't same-origin-local to the system browser. Note this has
    to be a policy hook on every navigation, not a link-click handler: the web UI starts the
    flow with `window.location.href = "/api/google/login"`, which the server then 302s to
    accounts.google.com — no link is ever clicked.
  - **The Edit menu.** A Cocoa app with no menu bar gets no Cmd+C/V/X/A/Z in its webview,
    because those are menu-driven actions, not key handling the webview does itself.
"""

from __future__ import annotations

import os
from typing import Callable
from urllib.parse import urlsplit

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSMenu,
    NSMenuItem,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSWorkspace,
)
from Foundation import NSMakeRect, NSMakeSize, NSObject, NSURL, NSURLRequest
from WebKit import (
    WKNavigationActionPolicyAllow,
    WKNavigationActionPolicyCancel,
    WKWebView,
    WKWebViewConfiguration,
)

WINDOW_SIZE = (1100.0, 760.0)
MIN_WINDOW_SIZE = (880.0, 600.0)

# Hosts that count as "our own server". Anything else is off-site and belongs in the browser.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Schemes the webview uses for its own internals. `about:` covers the splash, which is injected
# with loadHTMLString: and therefore navigates to about:blank.
_INTERNAL_SCHEMES = frozenset({"about", "data", "blob"})


def opens_in_window(url: str | None) -> bool:
    """Whether `url` should load in the app window, as opposed to the system browser.

    Split out as a plain function so the policy is unit-testable without a running Cocoa app.
    Fails closed: anything unparseable or unexpected goes to the browser rather than rendering
    inside Tempo.
    """
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme in _INTERNAL_SCHEMES:
        return True
    if parts.scheme not in ("http", "https"):
        return False
    try:
        host = parts.hostname
    except ValueError:  # malformed IPv6 literal, etc.
        return False
    return (host or "").lower() in _LOCAL_HOSTS


def open_externally(url: str) -> None:
    """Hand a URL to the user's default browser."""
    target = NSURL.URLWithString_(url)
    if target is not None:
        NSWorkspace.sharedWorkspace().openURL_(target)


# The navigation policy method's own Objective-C type encoding. pyobjc types a plain Python
# method's arguments as objects, but the third one is a *block* — and an incoming block with no
# declared signature is not callable, so calling it raises TypeError inside a WebKit callback,
# which surfaces as an uncaught ObjC exception and kills the app instantly. `@?<v@?q>` is
# pyobjc's inline block signature: void (^)(WKNavigationActionPolicy), where the leading `@?`
# inside the brackets is the block itself and `q` is the NSInteger-valued policy enum.
#
# Registering the same shape via objc.registerMetaDataForSelector does *not* work here: pyobjc
# already carries that metadata for this selector, yet still hands over a signature-less block.
_DECIDE_POLICY_SIGNATURE = b"v@:@@@?<v@?q>"


def _protocols() -> list:
    """Formal conformance for NSApplicationDelegate only.

    It's what types `applicationShouldTerminateAfterLastWindowClosed_` as returning BOOL rather
    than an object. The WebKit delegates are deliberately left informal — AppKit and WebKit
    both dispatch through respondsToSelector:, and declaring WKNavigationDelegate conformance
    would reject the explicit block signature above.
    """
    try:
        return [objc.protocolNamed("NSApplicationDelegate")]
    except Exception:
        return []


class TempoWebDelegate(NSObject, protocols=_protocols()):
    """Navigation policy, popup handling, and app teardown, in one object.

    Objective-C classes share one process-wide namespace, hence the prefixed name.
    """

    def initWithOnQuit_(self, on_quit):
        self = objc.super(TempoWebDelegate, self).init()
        if self is None:
            return None
        self._on_quit = on_quit
        return self

    # -- WKNavigationDelegate ---------------------------------------------------------------

    @objc.typedSelector(_DECIDE_POLICY_SIGNATURE)
    def webView_decidePolicyForNavigationAction_decisionHandler_(
        self, webview, action, decision_handler
    ):
        request = action.request()
        url = request.URL().absoluteString() if request.URL() is not None else None
        if opens_in_window(url):
            decision_handler(WKNavigationActionPolicyAllow)
            return
        decision_handler(WKNavigationActionPolicyCancel)
        if url:
            open_externally(url)

    # -- WKUIDelegate -----------------------------------------------------------------------

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
        self, webview, configuration, action, features
    ):
        """target="_blank" and window.open(): send them out rather than opening a blank window.

        Returning nil means "no new webview"; without this, such navigations are silently
        dropped because WKWebView has nowhere to put them.
        """
        request = action.request()
        url = request.URL().absoluteString() if request.URL() is not None else None
        if url:
            open_externally(url)
        return None

    # -- NSApplicationDelegate --------------------------------------------------------------

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        """Closing the window quits Tempo, matching how the shell behaved under Tauri."""
        return True

    def applicationWillTerminate_(self, notification):
        self._on_quit()


class Shell:
    """The app window. Build it, hand it to `run()`, and drive it with `show_app()`/`show_failed()`."""

    def __init__(self, base_url: str, splash_html: str, on_quit: Callable[[], None] | None = None):
        self.base_url = base_url
        self.splash_html = splash_html
        self._on_quit = on_quit or (lambda: None)
        self._app = None
        self._window = None
        self._webview = None
        self._delegate = None

    def build(self) -> None:
        """Create the application, menu bar, window and webview, and show the splash."""
        self._app = NSApplication.sharedApplication()
        # Required when launched as a bare script rather than from a .app bundle: without it
        # the process is an "accessory" with no Dock tile that can't take keyboard focus.
        self._app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        _install_menu_bar(self._app)

        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
        )
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, *WINDOW_SIZE), style, NSBackingStoreBuffered, False
        )
        self._window.setTitle_("Tempo")
        self._window.setMinSize_(NSMakeSize(*MIN_WINDOW_SIZE))
        self._window.center()

        config = WKWebViewConfiguration.alloc().init()
        if os.environ.get("TEMPO_DEV") == "1":
            # Right-click → Inspect Element. Off by default so shipped builds stay tidy.
            config.preferences().setValue_forKey_(True, "developerExtrasEnabled")
        self._webview = WKWebView.alloc().initWithFrame_configuration_(
            self._window.contentView().bounds(), config
        )
        self._delegate = TempoWebDelegate.alloc().initWithOnQuit_(self._on_quit)
        self._webview.setNavigationDelegate_(self._delegate)
        self._webview.setUIDelegate_(self._delegate)
        self._app.setDelegate_(self._delegate)
        # As the content view the webview tracks window resizes on its own.
        self._window.setContentView_(self._webview)

        self._webview.loadHTMLString_baseURL_(self.splash_html, None)
        self._window.makeKeyAndOrderFront_(None)
        self._app.activateIgnoringOtherApps_(True)

    def show_app(self) -> None:
        """Swap the splash for the real UI. Main thread only."""
        url = NSURL.URLWithString_(self.base_url)
        if url is not None:
            self._webview.loadRequest_(NSURLRequest.requestWithURL_(url))

    def show_failed(self) -> None:
        """Turn the splash into its error state. Main thread only."""
        self._webview.evaluateJavaScript_completionHandler_(
            "document.body.classList.add('failed')", None
        )

    def run(self) -> None:
        """Enter the Cocoa run loop. Blocks until the app quits; must be the main thread."""
        self._app.run()


def _install_menu_bar(app) -> None:
    """A minimal but standard menu bar. The Edit menu is what makes copy/paste work at all."""
    main_menu = NSMenu.alloc().init()

    app_item = NSMenuItem.alloc().init()
    main_menu.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    app_menu.addItemWithTitle_action_keyEquivalent_(
        "About Tempo", "orderFrontStandardAboutPanel:", ""
    )
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItemWithTitle_action_keyEquivalent_("Hide Tempo", "hide:", "h")
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItemWithTitle_action_keyEquivalent_("Quit Tempo", "terminate:", "q")
    app_item.setSubmenu_(app_menu)

    edit_item = NSMenuItem.alloc().init()
    main_menu.addItem_(edit_item)
    edit_menu = NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (
        ("Undo", "undo:", "z"),
        ("Redo", "redo:", "Z"),
        (None, None, None),
        ("Cut", "cut:", "x"),
        ("Copy", "copy:", "c"),
        ("Paste", "paste:", "v"),
        ("Select All", "selectAll:", "a"),
    ):
        if title is None:
            edit_menu.addItem_(NSMenuItem.separatorItem())
        else:
            edit_menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
    edit_item.setSubmenu_(edit_menu)

    view_item = NSMenuItem.alloc().init()
    main_menu.addItem_(view_item)
    view_menu = NSMenu.alloc().initWithTitle_("View")
    view_menu.addItemWithTitle_action_keyEquivalent_("Reload", "reload:", "r")
    view_item.setSubmenu_(view_menu)

    app.setMainMenu_(main_menu)
