"""PyInstaller entry point for the bundled Tempo server (the desktop sidecar).

PyInstaller needs a concrete script to analyze, and the frozen app needs port selection that
the `uvicorn` CLI doesn't give us: Google validates `redirect_uri` against an exact registered
string, so we can't bind an arbitrary free port the way a normal desktop sidecar would. We try
the registered ports in order instead, and tell the shell (and the user) which one we got.
"""

from __future__ import annotations

import argparse
import socket
import sys


def _is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _pick_port(host: str, candidates: tuple[int, ...]) -> int | None:
    for port in candidates:
        if _is_free(host, port):
            return port
    return None


def main() -> int:
    from backend.app import config

    parser = argparse.ArgumentParser(prog="tempo-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind this exact port. Omitted: first free registered port.",
    )
    args = parser.parse_args()

    if args.port is not None:
        port = args.port
        if not _is_free(args.host, port):
            print(f"[tempo] port {port} is already in use", file=sys.stderr)
            return 1
    else:
        port = _pick_port(args.host, config.CANDIDATE_PORTS)
        if port is None:
            ports = ", ".join(str(p) for p in config.CANDIDATE_PORTS)
            print(
                f"[tempo] no free port among {ports} — quit whatever is using them and retry.",
                file=sys.stderr,
            )
            return 1

    # Must happen before the app module is imported so the OAuth redirect URI is built with
    # the right port from the very first request.
    config.set_runtime_port(port)

    import uvicorn

    from backend.app.main import app

    print(f"[tempo] http://localhost:{port}", flush=True)
    uvicorn.run(app, host=args.host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
