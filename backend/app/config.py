"""Local-first settings: the Together key, the Google Calendar OAuth token, and the model.

Secrets live in the macOS Keychain when it's available and fall back to a 0600 JSON file
under the user's home; non-secret preferences (the model) always live in the JSON file.
Env vars override both, so you can still run headless without saving anything to disk.
Nothing leaves the machine except calls to Together and Google, which the user configures here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import keychain
from .paths import resource_dir

STATE_DIR = Path(os.environ.get("TEMPO_STATE_DIR", Path.home() / ".tempo"))
SETTINGS_FILE = STATE_DIR / "settings.json"

# Fallback model id used only until the user picks one from the live list in Settings
# (the Settings dropdown is populated from the account's actual Together models). Kept to a
# commonly-serverless Llama; if it isn't available on your plan, just pick another.
DEFAULT_MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"

# Values never written to settings.json when the Keychain is available.
SECRET_KEYS = ("together_key", "google_access_token", "google_refresh_token")

# Env fallbacks so you can run headless without saving anything to disk.
_ENV = {
    "together_key": "TOGETHER_API_KEY",
    "google_access_token": "GOOGLE_ACCESS_TOKEN",
    "model": "TEMPO_MODEL",
}

# Ports the packaged app may bind, in preference order. Google validates redirect_uri against
# an exact registered string, so every port here must be registered in the Cloud Console as
# `http://localhost:<port>/api/google/callback`. More than one so a busy 8000 doesn't make the
# app unlaunchable; few enough that registering them by hand stays reasonable.
CANDIDATE_PORTS = (8000, 8317, 8318, 8319)

_runtime_port = CANDIDATE_PORTS[0]


def set_runtime_port(port: int) -> None:
    """Record the port the server actually bound, so the OAuth redirect URI matches it."""
    global _runtime_port
    _runtime_port = int(port)


def runtime_port() -> int:
    return _runtime_port


# -- settings file + keychain -------------------------------------------------------------
def _read_file() -> dict[str, Any]:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _write_file(data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))
    try:
        SETTINGS_FILE.chmod(0o600)  # may still hold secrets when the Keychain is unavailable
    except OSError:
        pass


def load_settings() -> dict[str, Any]:
    data = _read_file()
    # Keychain wins over the file: a migrated secret is removed from the file, but an older
    # copy left behind by a crash mid-migration must never shadow the current value.
    for key in SECRET_KEYS:
        value = keychain.get(key)
        if value:
            data[key] = value
    # env fills any gap (never overwrites a saved value)
    for key, env in _ENV.items():
        if not data.get(key) and os.environ.get(env):
            data[key] = os.environ[env]
    data.setdefault("model", DEFAULT_MODEL)
    return data


def save_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """Merge non-empty updates: secrets to the Keychain, everything else to settings.json."""
    current = _read_file()
    for key, value in updates.items():
        if not value:  # blank = "leave unchanged" — a saved secret is never wiped by an empty field
            continue
        if key in SECRET_KEYS and keychain.set(key, str(value)):
            current.pop(key, None)  # migrate: the file must not keep a stale plaintext copy
        else:
            current[key] = value
    _write_file(current)
    return public_settings()


def forget(keys: list[str]) -> dict[str, Any]:
    """Delete keys from both stores (used by Google disconnect)."""
    current = _read_file()
    for k in keys:
        current.pop(k, None)
        if k in SECRET_KEYS:
            keychain.delete(k)
    if SETTINGS_FILE.exists():
        _write_file(current)
    return public_settings()


# -- Google OAuth client credentials -------------------------------------------------------
# Source order: env (dev, from .env) → the client baked into the bundle at build time.
# A packaged app has no repo-root .env, so without the baked file every install would launch
# with "Connect Google Calendar" permanently disabled. Desktop OAuth clients are public
# clients — Google treats the secret as non-confidential — so shipping it is expected.
_BUNDLED_CLIENT_FILE = "oauth_client.json"
_bundled_cache: dict[str, str] | None = None


def _bundled_client() -> dict[str, str]:
    global _bundled_cache
    if _bundled_cache is None:
        path = resource_dir() / _BUNDLED_CLIENT_FILE
        try:
            raw = json.loads(path.read_text())
            _bundled_cache = {
                "client_id": str(raw.get("client_id", "")),
                "client_secret": str(raw.get("client_secret", "")),
            }
        except (OSError, json.JSONDecodeError, AttributeError):
            _bundled_cache = {"client_id": "", "client_secret": ""}
    return _bundled_cache


def google_client_id() -> str:
    return (os.environ.get("GOOGLE_CLIENT_ID") or _bundled_client()["client_id"]).strip()


def google_client_secret() -> str:
    return (os.environ.get("GOOGLE_CLIENT_SECRET") or _bundled_client()["client_secret"]).strip()


def google_redirect_uri() -> str:
    """Must match a URI registered on the OAuth client — tracks the port we actually bound."""
    explicit = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    return f"http://localhost:{_runtime_port}/api/google/callback"


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
