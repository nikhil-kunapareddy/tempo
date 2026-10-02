#!/usr/bin/env python3
"""Sign an update archive so installed copies of Tempo will accept it.

    python scripts/sign_update.py packaging/dist/Tempo_0.2.0_arm64.app.tar.gz

Writes `<archive>.sig`: one line, base64 of the raw 64-byte Ed25519 signature over the
archive's exact bytes. The app checks it against the public key it was built with before it
installs anything, so a tampered or mis-signed archive is refused rather than run.

The private key is a base64 raw 32-byte Ed25519 seed, taken from $TEMPO_UPDATE_PRIVATE_KEY
(CI, from a repo secret) or else secrets/tempo_update_key (a local build; gitignored).

Signing with a key whose public half isn't the one the app embeds would publish updates
nobody can install, so that is checked here, at build time, instead of on users' machines.
"""

from __future__ import annotations

import base64
import binascii
import os
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ROOT = Path(__file__).resolve().parent.parent
KEY_ENV = "TEMPO_UPDATE_PRIVATE_KEY"
KEY_FILE = ROOT / "secrets" / "tempo_update_key"

# The key installed copies verify against. Must match the one embedded in the app's updater
# (backend/app/); rotating it means shipping one release signed with the old key that
# carries the new one.
PUBLIC_KEY = "Qs3xC9PL8ugqLUeeUTqPf04kyPT1GPlcCO5o/QmrBqo="


class SigningError(Exception):
    pass


def _decode(value: str, size: int, what: str) -> bytes:
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise SigningError(f"{what} is not valid base64") from None
    if len(raw) != size:
        raise SigningError(f"{what} is {len(raw)} bytes, expected {size}")
    return raw


def _shown(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_private_key() -> Ed25519PrivateKey:
    """The signing key from the environment, else the local secrets file."""
    value = os.environ.get(KEY_ENV, "").strip()
    source = f"${KEY_ENV}"
    if not value and KEY_FILE.is_file():
        value = KEY_FILE.read_text(encoding="utf-8").strip()
        source = _shown(KEY_FILE)
    if not value:
        raise SigningError(f"no update signing key: set ${KEY_ENV} or create {_shown(KEY_FILE)}")
    return Ed25519PrivateKey.from_private_bytes(_decode(value, 32, f"the key in {source}"))


def public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def verify(data: bytes, signature_b64: str, public_key: str = PUBLIC_KEY) -> None:
    """Raise SigningError unless `signature_b64` is a valid signature of `data`."""
    pub = Ed25519PublicKey.from_public_bytes(_decode(public_key, 32, "the public key"))
    try:
        pub.verify(_decode(signature_b64, 64, "the signature"), data)
    except InvalidSignature:
        raise SigningError("signature does not verify against the public key") from None


def sign_archive(
    archive: Path, key: Ed25519PrivateKey, expected_public_key: str | None = PUBLIC_KEY
) -> Path:
    """Write `<archive>.sig` and return its path."""
    if expected_public_key is not None and public_key_b64(key) != expected_public_key:
        raise SigningError(
            "the signing key's public half is not the one the app embeds "
            f"({expected_public_key}); installed copies would refuse this update"
        )
    data = archive.read_bytes()
    signature = base64.b64encode(key.sign(data)).decode("ascii")
    verify(data, signature, public_key_b64(key))
    out = archive.with_name(archive.name + ".sig")
    out.write_text(signature + "\n", encoding="ascii")
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: sign_update.py <archive>", file=sys.stderr)
        return 2
    archive = Path(args[0])
    try:
        if not archive.is_file():
            raise SigningError(f"{archive} does not exist")
        out = sign_archive(archive, load_private_key())
    except SigningError as exc:
        print(f"sign_update: {exc}", file=sys.stderr)
        return 1
    print(f"    signed {archive.name} → {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
