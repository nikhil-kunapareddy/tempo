"""Tempo's entry point: runs the server and the desktop window in one process.

This replaces the former Rust/Tauri shell, which had to spawn the Python server as a separate
frozen sidecar and hand it a port over argv. Now that the shell is Python too, the server is
just a thread:

  1. pick one of the OAuth-registered ports and tell `config` which one we got,
  2. start uvicorn on a daemon thread,
  3. show a splash window while it boots, then point the webview at http://localhost:<port> —
     the UI talks to the API with relative URLs, so loading it same-origin means the web
     frontend needs no changes at all,
  4. once it's up, look for a newer release in the background (backend/app/updater.py),
  5. ask the server to exit when the app quits.

The window has to own the main thread (AppKit's run loop is main-thread-only), which is why
uvicorn is the part that moves to a thread and not the other way around.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

# `console=False` in the PyInstaller spec is what keeps a packaged .app from flashing a
# terminal, and it leaves sys.stdout/sys.stderr as None. uvicorn logs through them, so they
# must be pointed somewhere real before anything imports it.
STARTUP_TIMEOUT = 45.0  # generous: a frozen bundle's first launch pays a cold page-in cost
LOG_NAME = "tempo.log"


def _log_dir() -> Path:
    from backend.app import config

    return config.STATE_DIR / "logs"


def _redirect_output() -> None:
    """Send stdout/stderr to ~/.tempo/logs/tempo.log, keeping the previous run as .old.

    A windowed app has no console, and losing the server's logs makes field reports
    undebuggable. Only rebinds when there's nothing usable attached, so running from a
    terminal still prints to the terminal.
    """
    if sys.stdout is not None and sys.stderr is not None and not getattr(sys, "frozen", False):
        return
    try:
        directory = _log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / LOG_NAME
        if path.exists():
            path.replace(directory / f"{LOG_NAME}.old")
        handle = open(path, "w", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return  # not being able to log is not a reason to refuse to start
    sys.stdout = handle
    sys.stderr = handle


def _splash_html() -> str:
    from backend.app.paths import resource_dir

    return (resource_dir() / "desktop" / "splash" / "index.html").read_text(encoding="utf-8")


def _build_server(host: str, port: int):
    """A configured, not-yet-running uvicorn server for the FastAPI app."""
    import uvicorn

    from backend.app.main import app

    return uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))


def _serve_headless(host: str, port: int) -> int:
    server = _build_server(host, port)
    print(f"[tempo] http://localhost:{port}", flush=True)
    server.run()
    return 0


def _serve_windowed(host: str, port: int) -> int:
    from AppKit import NSApplication
    from PyObjCTools import AppHelper

    from backend.app import updater

    from . import shell

    server = _build_server(host, port)
    thread = threading.Thread(target=server.run, name="tempo-server", daemon=True)

    def stop_server() -> None:
        server.should_exit = True
        thread.join(timeout=3.0)

    window = shell.Shell(
        base_url=f"http://localhost:{port}",
        splash_html=_splash_html(),
        on_quit=stop_server,
    )
    window.build()

    def quit_app() -> None:
        """End the app the way Cmd+Q does, so applicationWillTerminate_ stops uvicorn."""
        AppHelper.callAfter(lambda: NSApplication.sharedApplication().terminate_(None))

    # "Restart to update" hands off to a swap script and then needs Tempo gone.
    updater.set_quit_handler(quit_app)

    thread.start()
    print(f"[tempo] http://localhost:{port}", flush=True)

    def wait_then_show() -> None:
        """Swap in the real UI once uvicorn is serving. Runs off the main thread."""
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if server.started:
                AppHelper.callAfter(window.show_app)
                updater.start_check()  # a background thread; never touches the window itself
                return
            if not thread.is_alive():
                break  # uvicorn died during startup; no point waiting out the timeout
            time.sleep(0.1)
        print("[tempo] server did not come up", file=sys.stderr, flush=True)
        AppHelper.callAfter(window.show_failed)

    threading.Thread(target=wait_then_show, name="tempo-startup", daemon=True).start()
    window.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    _redirect_output()

    parser = argparse.ArgumentParser(prog="tempo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind this exact port. Omitted: first free registered port.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run only the server, with no window (open http://localhost:<port> yourself).",
    )
    args = parser.parse_args(argv)

    from backend.app import config

    if args.port is not None:
        port = args.port
        if not config.port_is_free(args.host, port):
            print(f"[tempo] port {port} is already in use", file=sys.stderr)
            return 1
    else:
        port = config.pick_free_port(args.host)
        if port is None:
            ports = ", ".join(str(p) for p in config.CANDIDATE_PORTS)
            print(
                f"[tempo] no free port among {ports} — quit whatever is using them and retry.",
                file=sys.stderr,
            )
            return 1

    # Both must be set before backend.app.main is imported, so the OAuth redirect URI is built
    # with the right port from the very first request.
    config.set_runtime_port(port)
    if not args.headless:
        # Tells the server it's hosting the desktop shell, so the OAuth callback renders a
        # "return to Tempo" page instead of redirecting a stray browser tab into the app.
        os.environ["TEMPO_DESKTOP"] = "1"

    if args.headless:
        return _serve_headless(args.host, port)
    return _serve_windowed(args.host, port)


if __name__ == "__main__":
    raise SystemExit(main())
