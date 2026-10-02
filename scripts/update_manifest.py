#!/usr/bin/env python3
"""Write latest.json, the file installed copies of Tempo read to find updates.

    python scripts/update_manifest.py <tag> <archive> > latest.json

Reads `<archive>.sig` (from sign_update.py) next to the archive. CI runs this once the .dmg
and the update archive are attached to the release, and attaches the result beside them. The
app asks for releases/latest/download/latest.json, which GitHub resolves to the release marked
Latest, so an update reaches people only once a stable release is promoted; pre-releases carry
a latest.json that nothing reads.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release  # noqa: E402
import sign_update  # noqa: E402

# The updater's name for the one platform Tempo ships: Apple Silicon macOS.
PLATFORM = "darwin-aarch64"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    tag: str,
    archive: Path,
    version: str,
    *,
    public_key: str | None = sign_update.PUBLIC_KEY,
    pub_date: datetime | None = None,
) -> dict:
    release.check_tag(tag, version)
    sig_path = archive.with_name(archive.name + ".sig")
    if not sig_path.is_file():
        raise release.ReleaseError(f"no signature at {sig_path}; run sign_update.py first")
    signature = sig_path.read_text(encoding="ascii").strip()
    # Fail here, not on every installed copy, if the archive and signature don't match.
    if public_key is not None:
        try:
            sign_update.verify(archive.read_bytes(), signature, public_key)
        except sign_update.SigningError as exc:
            raise release.ReleaseError(f"{sig_path.name}: {exc}") from None
    when = (pub_date or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "version": version,
        "notes": f"Tempo {tag}",
        "pub_date": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": {
            PLATFORM: {
                "url": f"https://github.com/{release.REPO}/releases/download/{quote(tag)}/{quote(archive.name)}",
                "signature": signature,
                "sha256": sha256_of(archive),
            }
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: update_manifest.py <tag> <archive>", file=sys.stderr)
        return 2
    tag, archive = args[0], Path(args[1])
    try:
        if not archive.is_file():
            raise release.ReleaseError(f"{archive} does not exist")
        manifest = build_manifest(tag, archive, release.read_version())
    except release.ReleaseError as exc:
        print(f"update_manifest: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
