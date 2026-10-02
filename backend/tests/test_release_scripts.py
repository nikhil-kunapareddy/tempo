"""The release scripts: version parsing, tag rules, update signing and latest.json.

Loaded by path, not imported as a package — `scripts/` is a folder of command-line tools. No
network and no git: next_tag() takes the tag list as an argument for exactly this reason.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    # Through sys.path, the way update_manifest imports its siblings when run as a script, so
    # the tests and update_manifest share one `release` module (and one ReleaseError class).
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


release = _load("release")
sign_update = _load("sign_update")
update_manifest = _load("update_manifest")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture
def throwaway_key(monkeypatch, tmp_path):
    """A fresh key in $TEMPO_UPDATE_PRIVATE_KEY, with the real secrets file out of reach."""
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    monkeypatch.setenv("TEMPO_UPDATE_PRIVATE_KEY", _b64(seed))
    monkeypatch.setattr(sign_update, "KEY_FILE", tmp_path / "no-such-key")
    return key


# -- version + tags ------------------------------------------------------------------------
def test_reads_the_real_version():
    assert release.read_version() == release.read_version(ROOT / "desktop" / "__init__.py")
    assert release.TAG_RE.match(f"v{release.read_version()}")


def test_read_version_parses_without_importing(tmp_path):
    f = tmp_path / "__init__.py"
    f.write_text('"""doc"""\nimport does_not_exist\n__version__ = "1.2.3"\n')
    assert release.read_version(f) == "1.2.3"


@pytest.mark.parametrize("body", ["x = 1\n", '__version__ = "1.2"\n', "__version__ = VERSION\n"])
def test_read_version_rejects_missing_or_malformed(tmp_path, body):
    f = tmp_path / "__init__.py"
    f.write_text(body)
    with pytest.raises(release.ReleaseError):
        release.read_version(f)


@pytest.mark.parametrize("tag", ["v0.2.0", "v0.2.0-beta.1", "v0.2.0-beta.12", "v0.2.0-rc.3"])
def test_check_tag_accepts_release_tags(tag):
    release.check_tag(tag, "0.2.0")


@pytest.mark.parametrize(
    "tag",
    ["0.2.0", "v0.2", "v0.2.0-alpha.1", "v0.2.0-beta", "v0.2.0-beta.0", "v0.2.0-beta.1x", "v0.2.0 "],
)
def test_check_tag_rejects_malformed(tag):
    with pytest.raises(release.ReleaseError, match="not a release tag"):
        release.check_tag(tag, "0.2.0")


def test_check_tag_rejects_a_different_version():
    with pytest.raises(release.ReleaseError, match="desktop/__init__.py says 0.2.0"):
        release.check_tag("v0.3.0-beta.1", "0.2.0")


def test_check_tag_cli_exit_codes(capsys):
    version = release.read_version()
    assert release.main(["--check-tag", f"v{version}-beta.2"]) == 0
    assert release.main(["--check-tag", "v9.9.9"]) == 1
    assert "says" in capsys.readouterr().err


def test_next_tag_numbers_each_channel():
    tags = ["v0.1.0", "v0.2.0-beta.1", "v0.2.0-beta.2", "v0.2.0-beta.10", "v0.2.0-rc.1", "v0.3.0-beta.7"]
    assert release.next_tag("0.2.0", "beta", tags) == "v0.2.0-beta.11"
    assert release.next_tag("0.2.0", "rc", tags) == "v0.2.0-rc.2"
    assert release.next_tag("0.4.0", "beta", tags) == "v0.4.0-beta.1"
    assert release.next_tag("0.2.0", "stable", tags) == "v0.2.0"


def test_next_tag_refuses_an_existing_stable():
    with pytest.raises(release.ReleaseError, match="already exists"):
        release.next_tag("0.1.0", "stable", ["v0.1.0"])


def test_next_tag_rejects_unknown_channel():
    with pytest.raises(release.ReleaseError, match="unknown channel"):
        release.next_tag("0.1.0", "nightly", [])


# -- signing --------------------------------------------------------------------------------
def test_contract_public_key_is_well_formed():
    assert len(base64.b64decode(sign_update.PUBLIC_KEY, validate=True)) == 32


def test_sign_then_verify(tmp_path, throwaway_key):
    archive = tmp_path / "Tempo_0.2.0_arm64.app.tar.gz"
    archive.write_bytes(b"pretend this is a tarball")
    pub = sign_update.public_key_b64(throwaway_key)

    sig_path = sign_update.sign_archive(archive, sign_update.load_private_key(), pub)

    assert sig_path.name == archive.name + ".sig"
    text = sig_path.read_text()
    assert text.endswith("\n") and text.count("\n") == 1
    assert len(base64.b64decode(text.strip())) == 64
    throwaway_key.public_key().verify(base64.b64decode(text), archive.read_bytes())
    sign_update.verify(archive.read_bytes(), text, pub)
    with pytest.raises(sign_update.SigningError):
        sign_update.verify(archive.read_bytes() + b"!", text, pub)


def test_refuses_a_key_the_app_does_not_embed(tmp_path, throwaway_key):
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(b"x")
    with pytest.raises(sign_update.SigningError, match="not the one the app embeds"):
        sign_update.sign_archive(archive, sign_update.load_private_key())  # default: contract key
    assert not (tmp_path / "a.tar.gz.sig").exists()


def test_no_key_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.delenv("TEMPO_UPDATE_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(sign_update, "KEY_FILE", tmp_path / "no-such-key")
    with pytest.raises(sign_update.SigningError, match="no update signing key"):
        sign_update.load_private_key()


def test_malformed_key_is_rejected(monkeypatch):
    monkeypatch.setenv("TEMPO_UPDATE_PRIVATE_KEY", _b64(b"too short"))
    with pytest.raises(sign_update.SigningError, match="expected 32"):
        sign_update.load_private_key()


# -- latest.json ----------------------------------------------------------------------------
def test_manifest_round_trip(tmp_path, throwaway_key):
    archive = tmp_path / "Tempo_0.2.0_arm64 test.app.tar.gz"  # a space, to exercise encoding
    archive.write_bytes(b"\x1f\x8b archive bytes")
    pub = sign_update.public_key_b64(throwaway_key)
    sign_update.sign_archive(archive, throwaway_key, pub)
    when = datetime(2026, 10, 1, 12, 30, 5, tzinfo=timezone.utc)

    manifest = update_manifest.build_manifest(
        "v0.2.0-beta.3", archive, "0.2.0", public_key=pub, pub_date=when
    )

    assert json.loads(json.dumps(manifest)) == manifest
    assert manifest["version"] == "0.2.0"
    assert manifest["notes"] == "Tempo v0.2.0-beta.3"
    assert manifest["pub_date"] == "2026-10-01T12:30:05Z"
    assert list(manifest["platforms"]) == ["darwin-aarch64"]
    entry = manifest["platforms"]["darwin-aarch64"]
    assert entry["url"] == (
        "https://github.com/nikhil-kunapareddy/tempo/releases/download/"
        "v0.2.0-beta.3/Tempo_0.2.0_arm64%20test.app.tar.gz"
    )
    assert entry["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    # What the app will do with it: check the signature against its embedded key.
    throwaway_key.public_key().verify(base64.b64decode(entry["signature"]), archive.read_bytes())


def test_manifest_refuses_a_mismatched_tag(tmp_path, throwaway_key):
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(b"x")
    sign_update.sign_archive(archive, throwaway_key, None)
    with pytest.raises(release.ReleaseError, match="says 0.2.0"):
        update_manifest.build_manifest("v0.3.0", archive, "0.2.0", public_key=None)


def test_manifest_needs_a_signature(tmp_path):
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(b"x")
    with pytest.raises(release.ReleaseError, match="no signature"):
        update_manifest.build_manifest("v0.2.0", archive, "0.2.0", public_key=None)


def test_manifest_refuses_a_signature_for_other_bytes(tmp_path, throwaway_key):
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(b"original")
    pub = sign_update.public_key_b64(throwaway_key)
    sign_update.sign_archive(archive, throwaway_key, pub)
    archive.write_bytes(b"rebuilt after signing")
    with pytest.raises(release.ReleaseError, match="does not verify"):
        update_manifest.build_manifest("v0.2.0", archive, "0.2.0", public_key=pub)
