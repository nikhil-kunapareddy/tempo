#!/usr/bin/env bash
# Build Tempo.app + a drag-to-install .dmg (Apple Silicon).
#
#   1. Bake the Google OAuth client into packaging/oauth_client.json (a packaged app has no
#      repo-root .env, so without this "Connect Google Calendar" is dead on arrival).
#   2. PyInstaller-freeze the server into a standalone onedir bundle.
#   3. Stage it at desktop/binaries/sidecar/ for Tauri's `resources` slot (+ sign its Mach-Os).
#   4. `tauri build --bundles app` → Tempo.app.
#   5. Wrap the .app in a compressed .dmg via hdiutil, then sign → notarize → staple.
#
# Prerequisites:
#   - Rust (rustup) and the Tauri CLI: cargo install tauri-cli --version "^2"
#   - A Python venv at .venv with the app's deps plus pyinstaller:
#       python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pyinstaller
#   - GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in the environment or in .env
#
# SIGNING: set APPLE_SIGNING_IDENTITY to a "Developer ID Application: … (TEAMID)" identity.
# Left unset → UNSIGNED build: it runs locally, but anyone you send it to gets Gatekeeper's
# "Apple could not verify…" dialog and has to allow it in System Settings → Privacy & Security.
#
# NOTARIZATION (runs only when the identity is set): signing alone is NOT enough for a public
# download. Auth is an App Store Connect API key via NOTARYTOOL_API_KEY_PATH /
# NOTARYTOOL_API_KEY_ID / NOTARYTOOL_API_ISSUER_ID. Missing → the DMG is still produced, with
# a loud warning. Set TEMPO_SKIP_NOTARIZE=1 to sign but skip the slow notary round-trip.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DESKTOP="$ROOT/desktop"
APP="Tempo"
VERSION="$(node -p "require('$DESKTOP/tauri.conf.json').version")"
TRIPLE="$(rustc -vV | sed -n 's/host: //p')"   # e.g. aarch64-apple-darwin
ARCH="${TRIPLE%%-*}"

if [ "$ARCH" != "aarch64" ]; then
  echo "ERROR: Tempo targets Apple Silicon only (host is $ARCH)." >&2
  exit 1
fi

echo "==> [1/5] baking the Google OAuth client"
# Env wins; otherwise read .env. Never committed — the file is gitignored.
TEMPO_ROOT="$ROOT" "$ROOT/.venv/bin/python" - <<'PY'
import json, os, pathlib
root = pathlib.Path(os.environ["TEMPO_ROOT"])
cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
sec = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
if not (cid and sec):
    try:
        from dotenv import dotenv_values
        env = dotenv_values(root / ".env")
        cid = cid or (env.get("GOOGLE_CLIENT_ID") or "").strip()
        sec = sec or (env.get("GOOGLE_CLIENT_SECRET") or "").strip()
    except ImportError:
        pass
if not (cid and sec):
    raise SystemExit("ERROR: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not found in env or .env")
out = root / "packaging" / "oauth_client.json"
out.write_text(json.dumps({"client_id": cid, "client_secret": sec}, indent=2))
out.chmod(0o600)
print(f"    baked client {cid[:12]}…")
PY

echo "==> [2/5] PyInstaller: freezing tempo-server ($TRIPLE)"
"$ROOT/.venv/bin/pyinstaller" --noconfirm --clean \
  --distpath "$HERE/dist" --workpath "$HERE/build" "$HERE/tempo-server.spec"

echo "==> [3/5] staging sidecar resources"
mkdir -p "$DESKTOP/binaries"
rm -rf "$DESKTOP/binaries/sidecar"
# -L (dereference): Tauri's resource bundler flattens symlinks into duplicate REAL files, so a
# symlinked framework arrives as a standalone copy whose signature no longer validates. Copy
# dereferenced up front, so what we SIGN is byte-identical to what Tauri COPIES.
cp -RL "$HERE/dist/tempo-server" "$DESKTOP/binaries/sidecar"
if [ -n "$(find "$DESKTOP/binaries/sidecar" -type l | head -1)" ]; then
  echo "ERROR: symlinks survived staging — Tauri would flatten them into unsigned copies" >&2
  exit 1
fi
# No *.framework may ship inside the sidecar: codesign/notarization infer bundle structure from
# the path and can never validate a flattened framework layout.
find "$DESKTOP/binaries/sidecar" -type d -name "*.framework" -exec rm -rf {} + 2>/dev/null || true
if [ -n "$(find "$DESKTOP/binaries/sidecar" -type d -name "*.framework" | head -1)" ]; then
  echo "ERROR: a .framework survived in the sidecar — it cannot pass notarization" >&2
  exit 1
fi
chmod +x "$DESKTOP/binaries/sidecar/tempo-server"

# Sign the sidecar's Mach-Os BEFORE tauri build: `tauri build` signs the .app (sealing resources
# into its signature) but does NOT sign nested binaries inside resources, and unsigned Mach-Os
# there fail notarization.
if [ -n "${APPLE_SIGNING_IDENTITY:-}" ]; then
  echo "    signing sidecar binaries"
  SIDECAR="$DESKTOP/binaries/sidecar"
  find "$SIDECAR" -type f ! -name "tempo-server" \
    ! -name "*.py" ! -name "*.pyc" ! -name "*.txt" ! -name "*.pem" ! -name "*.json" \
    -print0 | while IFS= read -r -d '' f; do
    file -b "$f" | grep -q "Mach-O" || continue
    codesign --force --sign "$APPLE_SIGNING_IDENTITY" --timestamp --options runtime "$f"
  done
  # Entitlements only on the entrypoint (disable-library-validation: the bundled Python dylibs
  # carry a different Team ID).
  codesign --force --sign "$APPLE_SIGNING_IDENTITY" --timestamp --options runtime \
    --entitlements "$DESKTOP/entitlements.plist" "$SIDECAR/tempo-server"
fi

echo "==> [4/5] tauri build (.app)"
( cd "$DESKTOP" && cargo tauri build --bundles app )

echo "==> [5/5] hdiutil: wrapping into .dmg"
BUNDLE="$DESKTOP/target/release/bundle"
STAGING="$(mktemp -d)"
cp -R "$BUNDLE/macos/$APP.app" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
DMG="$BUNDLE/dmg/${APP}_${VERSION}_${ARCH}.dmg"
mkdir -p "$(dirname "$DMG")"
rm -f "$DMG"
# Clear any stale mount so our image doesn't mount as "$APP 1".
[ -d "/Volumes/$APP" ] && hdiutil detach "/Volumes/$APP" -force >/dev/null 2>&1 || true
hdiutil create -volname "$APP" -srcfolder "$STAGING" -ov -format UDZO \
  -imagekey zlib-level=9 "$DMG" >/dev/null
rm -rf "$STAGING"

if [ -z "${APPLE_SIGNING_IDENTITY:-}" ]; then
  echo ""
  echo "    UNSIGNED build — fine for local use and hand-held testers, but every recipient"
  echo "    will see Gatekeeper's \"Apple could not verify\" dialog and must allow it under"
  echo "    System Settings → Privacy & Security. Set APPLE_SIGNING_IDENTITY to fix."
elif [ "${TEMPO_SKIP_NOTARIZE:-}" = "1" ]; then
  echo "    TEMPO_SKIP_NOTARIZE=1 — signing container, SKIPPING notarize (do not distribute)"
  codesign --sign "$APPLE_SIGNING_IDENTITY" --timestamp "$DMG"
else
  echo "    signing container → notarize → staple"
  codesign --sign "$APPLE_SIGNING_IDENTITY" --timestamp "$DMG"
  if [ -n "${NOTARYTOOL_API_KEY_PATH:-}" ] && [ -n "${NOTARYTOOL_API_KEY_ID:-}" ] \
     && [ -n "${NOTARYTOOL_API_ISSUER_ID:-}" ]; then
    xcrun notarytool submit "$DMG" \
      --key "$NOTARYTOOL_API_KEY_PATH" \
      --key-id "$NOTARYTOOL_API_KEY_ID" \
      --issuer "$NOTARYTOOL_API_ISSUER_ID" \
      --wait
    xcrun stapler staple "$DMG"
    # The same check Gatekeeper runs on download — fail here rather than ship a DMG that
    # greets users with the "Move to Trash" dialog.
    spctl -a -t open --context context:primary-signature "$DMG"
    echo "    Gatekeeper: accepted (notarized + stapled)"
  else
    echo "    WARNING: signed but NOT notarized — public downloads will see the 'Move to"
    echo "    Trash' dialog. Provide NOTARYTOOL_API_KEY_PATH/_KEY_ID/_ISSUER_ID."
  fi
fi

echo ""
echo "Done → $DMG"
