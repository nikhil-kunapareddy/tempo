"""Google OAuth 2.0 (authorization-code + refresh). Connect once via the browser; we store a
refresh token and mint fresh access tokens on demand, so the calendar tools never need a
hand-pasted token again. Client credentials come from env (GOOGLE_CLIENT_ID/SECRET)."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

from . import config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# Least privilege: read + create events (covers list/get/create on the primary calendar).
SCOPES = "https://www.googleapis.com/auth/calendar.events"


def build_auth_url(state: str) -> str:
    params = {
        "client_id": config.google_client_id(),
        "redirect_uri": config.google_redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",   # ask for a refresh token
        "prompt": "consent",        # force a refresh token even on re-consent
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict[str, Any]:
    resp = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": config.google_client_id(),
            "client_secret": config.google_client_secret(),
            "redirect_uri": config.google_redirect_uri(),
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _refresh(refresh_token: str) -> dict[str, Any]:
    resp = httpx.post(
        TOKEN_URL,
        data={
            "refresh_token": refresh_token,
            "client_id": config.google_client_id(),
            "client_secret": config.google_client_secret(),
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def store_tokens(token: dict[str, Any]) -> None:
    """Persist tokens from an exchange/refresh. Google omits refresh_token on refresh, so only
    write it when present (never clobber the stored one with an empty value)."""
    updates: dict[str, Any] = {}
    if token.get("access_token"):
        updates["google_access_token"] = token["access_token"]
        updates["google_token_expiry"] = time.time() + float(token.get("expires_in", 3600))
    if token.get("refresh_token"):
        updates["google_refresh_token"] = token["refresh_token"]
    config.save_settings(updates)


def get_valid_access_token() -> str:
    """A usable access token: the cached one if still fresh, otherwise refreshed via the
    stored refresh token. Falls back to a manually-set token (env). Raises if not connected."""
    s = config.load_settings()
    now = time.time()
    access_token = s.get("google_access_token")
    expiry = float(s.get("google_token_expiry") or 0)
    refresh_token = s.get("google_refresh_token")

    if access_token and now < expiry - 60:  # 60s safety margin
        return access_token
    if refresh_token and config.google_oauth_configured():
        token = _refresh(refresh_token)
        store_tokens(token)
        return token["access_token"]
    if access_token:  # manually pasted / env token, no refresh available
        return access_token
    raise RuntimeError(
        "Google Calendar not connected — click “Connect Google Calendar” in Settings."
    )


def disconnect() -> None:
    config.forget(["google_access_token", "google_token_expiry", "google_refresh_token"])
