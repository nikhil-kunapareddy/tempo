"""FastAPI backend: serves the minimal web UI and the chat/settings API. In this web-first
phase you run it with `uvicorn backend.app.main:app --reload` and open http://localhost:8000.
Phase 3 wraps this same server as a Tauri sidecar."""

from __future__ import annotations

import secrets as _secrets
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

# Load repo-root .env so GOOGLE_CLIENT_ID/SECRET, TOGETHER_API_KEY, etc. are available without
# manually exporting them. Done before the app reads any env.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from . import agent, config, google_oauth, together  # noqa: E402  (import after dotenv load)

# transient CSRF state for the OAuth round-trip (single local user)
_oauth_state: dict[str, bool] = {}

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

app = FastAPI(title="Tempo (Together + Google Calendar)")


class ChatIn(BaseModel):
    message: str
    history: list[dict[str, Any]] | None = None


class ApproveIn(BaseModel):
    messages: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    decision: str  # "approve" | "reject"


class SettingsIn(BaseModel):
    together_key: str | None = None
    google_access_token: str | None = None
    model: str | None = None


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return config.public_settings()


@app.post("/api/settings")
def post_settings(body: SettingsIn) -> dict[str, Any]:
    return config.save_settings(body.model_dump(exclude_none=True))


@app.get("/api/models")
def models() -> dict[str, Any]:
    """Chat models available to the configured Together key (for the Settings picker)."""
    try:
        return {"models": together.list_models()}
    except Exception as exc:
        return {"models": [], "error": f"Couldn't list Together models: {exc}"}


@app.get("/api/google/login")
def google_login():
    """Kick off the OAuth flow — redirect the browser to Google's consent screen."""
    if not config.google_oauth_configured():
        return HTMLResponse(
            "<p>Google OAuth isn't configured. Set <code>GOOGLE_CLIENT_ID</code> and "
            "<code>GOOGLE_CLIENT_SECRET</code> in the environment, then restart Tempo.</p>",
            status_code=400,
        )
    state = _secrets.token_urlsafe(24)
    _oauth_state[state] = True
    return RedirectResponse(google_oauth.build_auth_url(state))


@app.get("/api/google/callback")
def google_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    """Google redirects here with an auth code; exchange it for tokens, then return to the app."""
    if error:
        return HTMLResponse(f"<p>Google sign-in was cancelled or failed: {error}. You can close this tab.</p>")
    if not state or state not in _oauth_state:
        return HTMLResponse("<p>Invalid or expired sign-in state. Please try Connect again.</p>", status_code=400)
    _oauth_state.pop(state, None)
    if not code:
        return HTMLResponse("<p>No authorization code returned. Please try Connect again.</p>", status_code=400)
    try:
        google_oauth.store_tokens(google_oauth.exchange_code(code))
    except Exception as exc:
        return HTMLResponse(f"<p>Couldn't complete Google sign-in: {exc}</p>", status_code=502)
    return RedirectResponse("/")  # back into the app, now connected


@app.post("/api/google/disconnect")
def google_disconnect() -> dict[str, Any]:
    google_oauth.disconnect()
    return config.public_settings()


@app.post("/api/chat")
def chat(body: ChatIn) -> dict[str, Any]:
    try:
        return agent.run_agent(body.message, body.history)
    except RuntimeError as exc:  # missing key / not connected → friendly, not a 500
        return {"status": "error", "reply": None, "error": str(exc)}


@app.post("/api/approve")
def approve(body: ApproveIn) -> dict[str, Any]:
    """Resume a paused turn after the user approves/rejects the pending calendar write(s)."""
    try:
        return agent.resume(body.messages, body.pending, body.decision)
    except RuntimeError as exc:
        return {"status": "error", "reply": None, "error": str(exc)}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
