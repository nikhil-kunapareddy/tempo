"""Where files live at runtime — differs between a source checkout and a frozen bundle.

Under PyInstaller the source tree doesn't exist: `__file__` points inside `_internal/`, so any
`Path(__file__).parents[N]` walk up to the repo root resolves to a directory that isn't there.
Everything that needs a bundled data file (the web UI, the baked OAuth client) must go through
`resource_dir()` instead.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def resource_dir() -> Path:
    """Root for bundled read-only data files.

    Frozen: PyInstaller's extraction dir (`_internal/` for a onedir build), where the spec's
    `datas` entries land. Source: the repo root, where `web/` already sits.
    """
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[2]
