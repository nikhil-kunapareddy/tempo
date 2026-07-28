"""macOS Keychain storage for Tempo's secrets (Together key, Google tokens).

Shipping a desktop app means the refresh token stops being "a file on my own laptop" and
becomes "a file on someone else's laptop" — plaintext under `~/.tempo/settings.json` is no
longer good enough. This wraps the `security` CLI so we need no third-party dependency and
no extra PyInstaller hidden imports.

Caveat, deliberately accepted: `security add-generic-password -w <secret>` puts the secret in
the process argument list, which is readable by other processes running as the same user for
the moment the call takes. That is still strictly better than a plaintext file readable at any
time, and this is a single-user local app. Moving to the Security framework via ctypes would
close the gap if we ever want it.

Disable with TEMPO_KEYCHAIN=0 (tests do this) to fall back to the settings file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

SERVICE = "com.tempo.desktop"


def available() -> bool:
    if os.environ.get("TEMPO_KEYCHAIN") == "0":
        return False
    return sys.platform == "darwin" and shutil.which("security") is not None


def get(account: str) -> str | None:
    """Read a secret, or None if absent/unreadable."""
    if not available():
        return None
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None  # not found is the common case, not an error
    value = out.stdout.strip()
    return value or None


def set(account: str, value: str) -> bool:  # noqa: A001 - mirrors get/delete naming
    """Store (or replace) a secret. Returns False if the keychain is unavailable."""
    if not available() or not value:
        return False
    try:
        out = subprocess.run(
            # -U updates in place instead of erroring when the item already exists.
            ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", account, "-w", value],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def delete(account: str) -> None:
    if not available():
        return
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", SERVICE, "-a", account],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass
