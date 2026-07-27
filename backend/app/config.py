"""Local-first settings: the Together key, the Google Calendar OAuth token, and the model
live in a JSON file under the user's home (or come from env vars). Nothing leaves the
machine except calls to Together and Google, which the user configures here."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

STATE_DIR = Path(os.environ.get("TEMPO_STATE_DIR", Path.home() / ".tempo"))
SETTINGS_FILE = STATE_DIR / "settings.json"

# Fallback model id used only until the user picks one from the live list in Settings
# (the Settings dropdown is populated from the account's actual Together models). Kept to a
# commonly-serverless Llama; if it isn't available on your plan, just pick another.
DEFAULT_MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"

# Env fallbacks so you can run headless without saving anything to disk.
_ENV = {
    "together_key": "TOGETHER_API_KEY",
    "google_access_token": "GOOGLE_ACCESS_TOKEN",
    "model": "TEMPO_MODEL",
}


def load_settings() -> dict[str, Any]:
    data: dict[str, Any] = {}
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
    # env fills any gap (never overwrites a saved value)
    for key, env in _ENV.items():
        if not data.get(key) and os.environ.get(env):
            data[key] = os.environ[env]
    data.setdefault("model", DEFAULT_MODEL)
    return data


def save_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """Merge non-empty updates into settings.json (0600). Returns merged public settings."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    current: dict[str, Any] = {}
    if SETTINGS_FILE.exists():
        try:
            current = json.loads(SETTINGS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            current = {}
    for key, value in updates.items():
        if value:  # blank = "leave unchanged" — a saved secret is never wiped by an empty field
            current[key] = value
    SETTINGS_FILE.write_text(json.dumps(current, indent=2))
    try:
        SETTINGS_FILE.chmod(0o600)  # it holds secrets
    except OSError:
        pass
    return public_settings()


def forget(keys: list[str]) -> dict[str, Any]:
    """Delete keys from settings.json (used by Google disconnect)."""
    if SETTINGS_FILE.exists():
        try:
            current = json.loads(SETTINGS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            current = {}
        for k in keys:
            current.pop(k, None)
        SETTINGS_FILE.write_text(json.dumps(current, indent=2))
    return public_settings()


# -- Google OAuth client credentials (env only; never persisted to settings.json) --------
def google_client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def google_client_secret() -> str:
    return os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


def google_redirect_uri() -> str:
    return os.environ.get(
        "GOOGLE_REDIRECT_URI", "http://localhost:8000/api/google/callback"
    ).strip()


def google_oauth_configured() -> bool:
    return bool(google_client_id() and google_client_secret())


def public_settings() -> dict[str, Any]:
    """Settings safe for the UI — booleans for secrets, never the secret values."""
    s = load_settings()
    return {
        "model": s.get("model", DEFAULT_MODEL),
        "together_key_set": bool(s.get("together_key")),
        "google_connected": bool(s.get("google_refresh_token") or s.get("google_access_token")),
        "google_oauth_configured": google_oauth_configured(),
    }
