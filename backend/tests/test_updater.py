"""The updater without a network or a real install: versions, the manifest, both download checks,
unpacking, the swap script, and the API the UI polls."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import plistlib
import subprocess
import threading
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from backend.app import config, updater

CURRENT = "0.1.0"
ARCHIVE_URL = "https://github.com/nikhil-kunapareddy/tempo/releases/download/v0.2.0/Tempo.app.tar.gz"


@pytest.fixture
def key():
    return Ed25519PrivateKey.generate()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch, key):
    """A throwaway state dir, a fake installed bundle, and a test signing key."""
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "state" / "settings.json")
    bundle = tmp_path / "Applications" / "Tempo.app"
    (bundle / "Contents").mkdir(parents=True)
    monkeypatch.setenv("TEMPO_UPDATE_FORCE", "1")
    monkeypatch.setenv("TEMPO_UPDATE_BUNDLE", str(bundle))
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(updater, "PUBLIC_KEY", base64.b64encode(public).decode())
    monkeypatch.setattr(updater, "_codesign_verify", lambda bundle: True)
    monkeypatch.setattr(updater, "current_version", lambda: CURRENT)
    return bundle


def make_archive(tmp_path, version, names=("Tempo.app",)) -> bytes:
    """A tar.gz shaped like the real one: top-level Tempo.app with an Info.plist."""
    src = tmp_path / f"src-{version}"
    for name in names:
        contents = src / name / "Contents"
        contents.mkdir(parents=True)
        with open(contents / "Info.plist", "wb") as handle:
            plistlib.dump({"CFBundleShortVersionString": version}, handle)
    out = tmp_path / f"archive-{version}.tar.gz"
    subprocess.run(
        ["/usr/bin/tar", "-czf", str(out), "-C", str(src), *names],
        check=True,
        env={**os.environ, "COPYFILE_DISABLE": "1"},
    )
    return out.read_bytes()


def manifest(key, data: bytes, version="0.2.0", **overrides) -> dict:
    entry = {
        "url": ARCHIVE_URL,
        "signature": base64.b64encode(key.sign(data)).decode(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    entry.update(overrides)
    return {"version": version, "notes": "Tempo v0.2.0", "pub_date": "2026-10-01T00:00:00Z",
            "platforms": {"darwin-aarch64": entry}}


def serve(monkeypatch, manifest_json=None, archive: bytes = b"", status=200):
    """Stand in for GitHub: httpx.get answers the manifest, httpx.stream the archive."""
    def fake_get(url, **kwargs):
        request = httpx.Request("GET", url)
        if manifest_json is None:
            return httpx.Response(404, request=request)
        return httpx.Response(status, json=manifest_json, request=request)

    @contextlib.contextmanager
    def fake_stream(method, url, **kwargs):
        yield httpx.Response(200, content=archive, request=httpx.Request(method, url))

    monkeypatch.setattr(updater.httpx, "get", fake_get)
    monkeypatch.setattr(updater.httpx, "stream", fake_stream)


# -- versions ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "older, newer",
    [
        ("0.2.0-beta.1", "0.2.0"),
        ("0.2.0-beta.1", "0.2.0-beta.2"),
        ("0.2.0-beta.9", "0.2.0-beta.10"),
        ("0.2.0-beta.3", "0.2.0-rc.1"),
        ("0.9.0", "0.10.0"),
        ("0.1.0", "0.1.1"),
        ("1.9.9", "2.0.0"),
        ("v0.1.0", "0.2.0"),
    ],
)
def test_version_ordering(older, newer):
    assert updater.is_newer(newer, older)
    assert not updater.is_newer(older, newer)


def test_equal_versions_are_not_an_update():
    assert not updater.is_newer("0.2.0", "0.2.0")
    assert not updater.is_newer("0.2.0+build.7", "0.2.0")


@pytest.mark.parametrize("bad", ["", "1.2", "1.2.3.4", "latest", "1.2.x"])
def test_invalid_versions_raise(bad):
    with pytest.raises(ValueError):
        updater.version_key(bad)


# -- the manifest --------------------------------------------------------------------------
def test_parse_manifest(key):
    release = updater.parse_manifest(manifest(key, b"payload"))
    assert release.version == "0.2.0"
    assert release.url == ARCHIVE_URL
    assert len(release.signature) == 64


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.pop("platforms"),
        lambda m: m["platforms"].pop("darwin-aarch64"),
        lambda m: m["platforms"].update({"darwin-aarch64": "nope"}),
        lambda m: m["platforms"]["darwin-aarch64"].pop("url"),
        lambda m: m["platforms"]["darwin-aarch64"].update(url="http://example.com/Tempo.app.tar.gz"),
        lambda m: m["platforms"]["darwin-aarch64"].pop("signature"),
        lambda m: m["platforms"]["darwin-aarch64"].update(signature="c2hvcnQ="),
        lambda m: m["platforms"]["darwin-aarch64"].update(signature="not base64!"),
        lambda m: m["platforms"]["darwin-aarch64"].update(sha256="abc"),
        lambda m: m.pop("version"),
        lambda m: m.update(version="soon"),
    ],
)
def test_parse_manifest_rejects(key, mutate):
    data = manifest(key, b"payload")
    mutate(data)
    with pytest.raises(updater.UpdateError):
        updater.parse_manifest(data)


def test_parse_manifest_rejects_non_object():
    with pytest.raises(updater.UpdateError):
        updater.parse_manifest(["not", "a", "manifest"])


def test_local_http_is_allowed_for_testing(key):
    data = manifest(key, b"payload")
    data["platforms"]["darwin-aarch64"]["url"] = "http://127.0.0.1:9000/Tempo.app.tar.gz"
    assert updater.parse_manifest(data).url.startswith("http://127.0.0.1")


def test_no_stable_release_yet_is_not_an_error(monkeypatch):
    serve(monkeypatch, manifest_json=None)
    assert updater.fetch_manifest() is None


def test_unreachable_github_is_an_update_error(monkeypatch):
    def boom(url, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(updater.httpx, "get", boom)
    with pytest.raises(updater.UpdateError):
        updater.fetch_manifest()


def test_manifest_url_override(monkeypatch):
    assert updater.manifest_url() == updater.MANIFEST_URL
    monkeypatch.setenv("TEMPO_UPDATE_MANIFEST_URL", "http://127.0.0.1:9/latest.json")
    assert updater.manifest_url() == "http://127.0.0.1:9/latest.json"


# -- download checks -----------------------------------------------------------------------
def test_good_signature_passes(tmp_path, key):
    archive = tmp_path / "Tempo.app.tar.gz"
    archive.write_bytes(b"the real thing")
    updater.verify_archive(archive, updater.parse_manifest(manifest(key, b"the real thing")))
    assert archive.exists()


def test_tampered_archive_is_rejected_and_deleted(tmp_path, key):
    archive = tmp_path / "Tempo.app.tar.gz"
    archive.write_bytes(b"something else")
    data = manifest(key, b"the real thing")
    data["platforms"]["darwin-aarch64"]["sha256"] = hashlib.sha256(b"something else").hexdigest()
    with pytest.raises(updater.UpdateError, match="signature"):
        updater.verify_archive(archive, updater.parse_manifest(data))
    assert not archive.exists()


def test_signature_from_another_key_is_rejected(tmp_path, key):
    archive = tmp_path / "Tempo.app.tar.gz"
    archive.write_bytes(b"the real thing")
    forged = manifest(Ed25519PrivateKey.generate(), b"the real thing")
    with pytest.raises(updater.UpdateError, match="signature"):
        updater.verify_archive(archive, updater.parse_manifest(forged))
    assert not archive.exists()


def test_checksum_mismatch_is_rejected_and_deleted(tmp_path, key):
    archive = tmp_path / "Tempo.app.tar.gz"
    archive.write_bytes(b"the real thing")
    data = manifest(key, b"the real thing", sha256="0" * 64)
    with pytest.raises(updater.UpdateError, match="checksum"):
        updater.verify_archive(archive, updater.parse_manifest(data))
    assert not archive.exists()


# -- unpacking -----------------------------------------------------------------------------
def _archive_file(tmp_path, data: bytes):
    path = tmp_path / "Tempo.app.tar.gz"
    path.write_bytes(data)
    return path


def test_stage_bundle(tmp_path):
    archive = _archive_file(tmp_path, make_archive(tmp_path, "0.2.0"))
    bundle = updater.stage_bundle(archive, tmp_path / "staged", "0.2.0")
    assert bundle == tmp_path / "staged" / "Tempo.app"
    assert (bundle / "Contents" / "Info.plist").exists()


def test_stage_bundle_rejects_wrong_version(tmp_path):
    archive = _archive_file(tmp_path, make_archive(tmp_path, "0.1.5"))
    with pytest.raises(updater.UpdateError, match="version"):
        updater.stage_bundle(archive, tmp_path / "staged", "0.2.0")


def test_stage_bundle_rejects_extra_entries(tmp_path):
    archive = _archive_file(tmp_path, make_archive(tmp_path, "0.2.0", names=("Tempo.app", "Other.app")))
    with pytest.raises(updater.UpdateError, match="Tempo.app"):
        updater.stage_bundle(archive, tmp_path / "staged", "0.2.0")


def test_stage_bundle_rejects_broken_code_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "_codesign_verify", lambda bundle: False)
    archive = _archive_file(tmp_path, make_archive(tmp_path, "0.2.0"))
    with pytest.raises(updater.UpdateError, match="signature"):
        updater.stage_bundle(archive, tmp_path / "staged", "0.2.0")


# -- where the app runs from ---------------------------------------------------------------
def test_writable_bundle_is_supported(env):
    assert updater.location_problem(env) is None


@pytest.mark.parametrize(
    "path",
    [
        "/private/var/folders/xy/T/AppTranslocation/1234/d/Tempo.app",
        "/Volumes/Tempo/Tempo.app",
    ],
)
def test_translocated_or_disk_image_is_unsupported(path):
    from pathlib import Path

    assert updater.location_problem(Path(path)) == updater.MOVE_TO_APPLICATIONS


def test_read_only_folder_is_unsupported(env):
    env.parent.chmod(0o500)
    try:
        assert "Applications folder" in updater.location_problem(env)
    finally:
        env.parent.chmod(0o700)


def test_source_run_is_unsupported(monkeypatch):
    monkeypatch.delenv("TEMPO_UPDATE_FORCE")
    u = updater.Updater()
    assert u.status()["state"] == "unsupported"
    assert u.start_check() is False


def test_disabled_when_setting_off():
    config.save_settings({"auto_update": False})
    u = updater.Updater()
    assert u.status()["state"] == "disabled"
    assert u.start_check() is False
    assert u.status()["state"] == "disabled"


# -- the whole check -----------------------------------------------------------------------
def test_check_downloads_verifies_and_stages(tmp_path, monkeypatch, key):
    data = make_archive(tmp_path, "0.2.0")
    serve(monkeypatch, manifest(key, data), data)
    stale = config.STATE_DIR / "updates" / "0.1.9"
    stale.mkdir(parents=True)

    u = updater.Updater()
    u.check()

    status = u.status()
    assert status == {"state": "ready", "current_version": CURRENT, "version": "0.2.0", "notes": "Tempo v0.2.0"}
    assert (config.STATE_DIR / "updates" / "0.2.0" / "staged" / "Tempo.app").is_dir()
    assert not stale.exists()


def test_check_runs_in_background(tmp_path, monkeypatch, key):
    data = make_archive(tmp_path, "0.2.0")
    serve(monkeypatch, manifest(key, data), data)
    u = updater.Updater()
    assert u.start_check() is True
    assert u.start_check() is False  # one check per launch
    deadline = time.monotonic() + 10
    while u.status()["state"] != "ready" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert u.status()["state"] == "ready"


def test_same_version_is_up_to_date(tmp_path, monkeypatch, key):
    data = make_archive(tmp_path, CURRENT)
    serve(monkeypatch, manifest(key, data, version=CURRENT), data)
    u = updater.Updater()
    u.check()
    assert u.status()["state"] == "up_to_date"


def test_no_release_is_up_to_date(monkeypatch):
    serve(monkeypatch, manifest_json=None)
    u = updater.Updater()
    u.check()
    assert u.status()["state"] == "up_to_date"


def test_bad_signature_ends_in_error_and_deletes_download(tmp_path, monkeypatch, key):
    data = make_archive(tmp_path, "0.2.0")
    serve(monkeypatch, manifest(Ed25519PrivateKey.generate(), data), data)
    u = updater.Updater()
    u.check()
    status = u.status()
    assert status["state"] == "error"
    assert "signature" in status["message"]
    assert not (config.STATE_DIR / "updates" / "0.2.0" / "Tempo.app.tar.gz").exists()


def test_unexpected_crash_is_contained(monkeypatch):
    def broken():
        raise KeyError("bug")

    monkeypatch.setattr(updater, "fetch_manifest", broken)
    u = updater.Updater()
    u.check()
    assert u.status()["state"] == "error"


# -- installing ----------------------------------------------------------------------------
def test_install_refused_unless_ready():
    u = updater.Updater()
    u.set_quit_handler(lambda: None)
    with pytest.raises(updater.UpdateError, match="No update"):
        u.install()


def test_install_spawns_swap_script_then_quits(tmp_path, monkeypatch, key, env):
    data = make_archive(tmp_path, "0.2.0")
    serve(monkeypatch, manifest(key, data), data)
    u = updater.Updater()
    u.check()
    quit_called = threading.Event()
    u.set_quit_handler(quit_called.set)
    spawned = []

    def fake_popen(args, **kwargs):
        spawned.append((args, kwargs))

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)
    u.install()

    (args, kwargs), = spawned
    staged = config.STATE_DIR / "updates" / "0.2.0" / "staged" / "Tempo.app"
    script = config.STATE_DIR / "updates" / "install-update.sh"
    assert args == ["/bin/bash", str(script), str(os.getpid()), str(env), str(staged),
                    str(config.STATE_DIR / "updates" / "previous" / "Tempo.app")]
    assert kwargs["start_new_session"] is True
    assert script.read_text().startswith("#!/bin/bash")
    assert quit_called.wait(2)
    with pytest.raises(updater.UpdateError, match="already"):
        u.install()


def test_install_without_quit_handler_is_refused(tmp_path, monkeypatch, key):
    data = make_archive(tmp_path, "0.2.0")
    serve(monkeypatch, manifest(key, data), data)
    u = updater.Updater()
    u.check()
    with pytest.raises(updater.UpdateError, match="reopen"):
        u.install()


def _fake_bundle(path, marker):
    (path / "Contents").mkdir(parents=True)
    (path / "Contents" / "marker").write_text(marker)


def _run_script(tmp_path, current, staged, previous):
    script = updater.write_install_script()
    waiting_on = subprocess.Popen(["/bin/sleep", "0.3"])
    # Reap it as soon as it exits, as launchd does for the real app: an unreaped zombie still
    # answers `kill -0`, and the script would wait out its full 30 seconds.
    threading.Thread(target=waiting_on.wait, daemon=True).start()
    result = subprocess.run(
        ["/bin/bash", str(script), str(waiting_on.pid), str(current), str(staged), str(previous)],
        env={**os.environ, "TEMPO_OPEN": "/usr/bin/true"},
        capture_output=True,
        text=True,
        timeout=40,
    )
    return result


def test_swap_script_replaces_bundle_and_keeps_previous(tmp_path):
    current = tmp_path / "Apps" / "Tempo.app"
    staged = tmp_path / "staged" / "Tempo.app"
    previous = tmp_path / "previous" / "Tempo.app"
    _fake_bundle(current, "old")
    _fake_bundle(staged, "new")
    _fake_bundle(previous, "older")

    result = _run_script(tmp_path, current, staged, previous)

    assert result.returncode == 0, result.stdout
    assert (current / "Contents" / "marker").read_text() == "new"
    assert (previous / "Contents" / "marker").read_text() == "old"
    assert not staged.exists()


def test_swap_script_leaves_app_alone_without_staged_bundle(tmp_path):
    current = tmp_path / "Apps" / "Tempo.app"
    _fake_bundle(current, "old")
    result = _run_script(tmp_path, current, tmp_path / "gone" / "Tempo.app", tmp_path / "previous" / "Tempo.app")
    assert result.returncode == 1
    assert (current / "Contents" / "marker").read_text() == "old"


def test_swap_script_refuses_non_bundle_paths(tmp_path):
    result = _run_script(tmp_path, tmp_path / "Apps", tmp_path / "staged" / "Tempo.app", tmp_path / "p" / "Tempo.app")
    assert result.returncode == 2


# -- the API -------------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    from backend.app.main import app

    fresh = updater.Updater()
    monkeypatch.setattr(updater, "status", fresh.status)
    monkeypatch.setattr(updater, "install", fresh.install)
    return TestClient(app)


def test_update_status_endpoint(client):
    body = client.get("/api/update").json()
    assert body == {"state": "idle", "current_version": CURRENT}


def test_install_endpoint_refused_unless_ready(client):
    response = client.post("/api/update/install")
    assert response.status_code == 409
    assert response.json()["ok"] is False


def test_auto_update_round_trips_through_settings(client):
    assert client.get("/api/settings").json()["auto_update"] is True
    assert client.post("/api/settings", json={"auto_update": False}).json()["auto_update"] is False
    assert client.get("/api/update").json()["state"] == "disabled"
    assert client.post("/api/settings", json={"auto_update": True}).json()["auto_update"] is True


def test_blank_secret_still_means_unchanged(client):
    client.post("/api/settings", json={"together_key": "sk-test"})
    client.post("/api/settings", json={"together_key": "", "auto_update": False})
    settings = client.get("/api/settings").json()
    assert settings["together_key_set"] is True
    assert settings["auto_update"] is False
