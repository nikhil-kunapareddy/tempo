"""Test-wide isolation: never touch the real Keychain or the real ~/.tempo.

Without this the settings tests would read and write the developer's actual Google tokens,
since config.load_settings() prefers Keychain values over the (monkeypatched) settings file.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_keychain(monkeypatch):
    monkeypatch.setenv("TEMPO_KEYCHAIN", "0")
