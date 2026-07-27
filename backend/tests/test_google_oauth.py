"""Google OAuth: the auth URL asks for a refresh token, and access tokens auto-refresh."""

from __future__ import annotations

import time

from backend.app import config, google_oauth


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "sec")


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_build_auth_url_requests_offline_refresh(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "sec")
    url = google_oauth.build_auth_url("st8")
    assert "client_id=cid" in url
    assert "access_type=offline" in url and "prompt=consent" in url
    assert "state=st8" in url
    assert "calendar.events" in url


def test_access_token_refreshes_when_expired(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    config.save_settings({"google_refresh_token": "rt", "google_access_token": "old"})
    monkeypatch.setattr(
        google_oauth.httpx, "post", lambda *a, **k: _Resp({"access_token": "new_at", "expires_in": 3600})
    )

    token = google_oauth.get_valid_access_token()

    assert token == "new_at"
    assert config.load_settings()["google_access_token"] == "new_at"


def test_fresh_cached_token_is_reused_without_network(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    config.save_settings({"google_access_token": "fresh", "google_token_expiry": time.time() + 3600})
    called: list = []
    monkeypatch.setattr(google_oauth.httpx, "post", lambda *a, **k: called.append(1))

    assert google_oauth.get_valid_access_token() == "fresh"
    assert called == []  # no refresh call needed


def test_not_connected_raises(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    config.save_settings({"model": "x"})  # nothing google
    try:
        google_oauth.get_valid_access_token()
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "not connected" in str(exc).lower()
