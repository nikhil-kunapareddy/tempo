#!/usr/bin/env bash
# Build Tempo.app, a drag-to-install .dmg and a signed update archive (Apple Silicon).
#
#   1. Bake the Google OAuth client into packaging/oauth_client.json (a packaged app has no
#      repo-root .env, so without this "Connect Google Calendar" is dead on arrival).
#   2. PyInstaller-freeze the whole app — server, Cocoa shell and web UI — into Tempo.app.
#   3. Code-sign: Developer ID inside-out when an identity is set, ad-hoc otherwise.
#   4. Tar the signed .app into the update archive installed copies download, and sign it.
#   5. Wrap the .app in a compressed .dmg via hdiutil, with the window layout from
#      desktop/installer/ (background + icon positions); with a Developer ID, sign → notarize →
#      staple.
#
# Output, in packaging/dist/:
#   Tempo_<ver>_arm64.dmg              what people download
#   Tempo_<ver>_arm64.app.tar.gz       what the in-app updater downloads
#   Tempo_<ver>_arm64.app.tar.gz.sig   its Ed25519 signature (only when a key is available)
#
# Prerequisites:
#   - A Python venv at .venv with the app's deps plus pyinstaller:
#       python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pyinstaller
#   - GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in the environment or in .env. CI builds of pull
#     requests have no secrets, so TEMPO_ALLOW_NO_OAUTH=1 builds without them (the app then
#     launches with "Connect Google Calendar" disabled — fine for a test build, not a release).
#
# No Rust/cargo/Tauri toolchain is needed any more: the shell is Python (desktop/shell.py) and
# PyInstaller's BUNDLE emits the .app directly.
#
# SIGNING: APPLE_SIGNING_IDENTITY unset → AD-HOC signed (`codesign --sign -`). It runs, and the
# bundle's resources are sealed, but Gatekeeper doesn't know the signer: the first launch on
# each Mac needs right-click → Open (the README says how). Not leaving it unsigned matters: a
# bundle with only the linker's ad-hoc signature on the executable has no resource seal and
# macOS refuses out-of-process services to it, such as the open/save panel.
# Set APPLE_SIGNING_IDENTITY to a "Developer ID Application: … (TEAMID)" identity to sign
# properly instead.
#
# NOTARIZATION (runs only when the identity is set): signing alone is NOT enough for a public
# download. Auth is an App Store Connect API key via NOTARYTOOL_API_KEY_PATH /
# NOTARYTOOL_API_KEY_ID / NOTARYTOOL_API_ISSUER_ID. Missing → the DMG is still produced, with
# a loud warning. Set TEMPO_SKIP_NOTARIZE=1 to sign but skip the slow notary round-trip.
#
# UPDATE SIGNING: the archive is signed by scripts/sign_update.py with $TEMPO_UPDATE_PRIVATE_KEY
# (CI) or secrets/tempo_update_key (local). Neither → the archive is left unsigned, which
# installed copies refuse; TEMPO_REQUIRE_UPDATE_SIGNATURE=1 (CI release builds) makes that an
# error instead.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="$ROOT/.venv/bin/python"
APP="Tempo"
DIST="$HERE/dist"
BUNDLE="$DIST/$APP.app"
VERSION="$("$PY" -c 'import sys; sys.path.insert(0, "'"$ROOT"'"); import desktop; print(desktop.__version__)')"
ARCH="$(uname -m)"

if [ "$ARCH" != "arm64" ]; then
  echo "ERROR: Tempo targets Apple Silicon only (host is $ARCH)." >&2
  exit 1
fi

echo "==> [1/5] baking the Google OAuth client"
# Env wins; otherwise read .env. Never committed — the file is gitignored.
TEMPO_ROOT="$ROOT" "$PY" - <<'PY'
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
    if os.environ.get("TEMPO_ALLOW_NO_OAUTH") == "1":
        print("    WARNING: no GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET — building without an OAuth")
        print("    client (TEMPO_ALLOW_NO_OAUTH=1). 'Connect Google Calendar' will be disabled.")
        raise SystemExit(0)
    raise SystemExit("ERROR: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not found in env or .env")
out = root / "packaging" / "oauth_client.json"
out.write_text(json.dumps({"client_id": cid, "client_secret": sec}, indent=2))
out.chmod(0o600)
print(f"    baked client {cid[:12]}…")
PY

echo "==> [2/5] PyInstaller: freezing $APP.app ($ARCH)"
rm -rf "$BUNDLE"
"$ROOT/.venv/bin/pyinstaller" --noconfirm --clean \
  --distpath "$DIST" --workpath "$HERE/build" "$HERE/tempo.spec"
test -d "$BUNDLE" || { echo "ERROR: $BUNDLE was not produced" >&2; exit 1; }

echo "==> [3/5] staging + code-signing"
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
STAGED="$STAGING/$APP.app"
# ditto, not cp -R: it strips extended attributes. A checkout inside an iCloud-synced folder
# (~/Documents) picks up com.apple.FinderInfo and com.apple.fileprovider on the bundle, and
# codesign refuses those outright with "resource fork, Finder information, or similar detritus
# not allowed" — including PyInstaller's own ad-hoc signing attempt during the build.
ditto --norsrc --noextattr --noqtn "$BUNDLE" "$STAGED"
if [ -n "${APPLE_SIGNING_IDENTITY:-}" ]; then
  # Inside-out: every nested Mach-O first, the bundle last. `codesign --deep` is explicitly
  # discouraged by Apple and gets the entitlements wrong on nested code.
  find "$STAGED/Contents" -type f -print0 | while IFS= read -r -d '' f; do
    [ "$f" = "$STAGED/Contents/MacOS/$APP" ] && continue
    file -b "$f" | grep -q "Mach-O" || continue
    codesign --force --sign "$APPLE_SIGNING_IDENTITY" --timestamp --options runtime "$f"
  done
  # Entitlements on the bundle: disable-library-validation is required because the bundled
  # Python dylibs carry a different Team ID. Accepted by notarization.
  codesign --force --sign "$APPLE_SIGNING_IDENTITY" --timestamp --options runtime \
    --entitlements "$ROOT/desktop/entitlements.plist" "$STAGED"
else
  # Ad-hoc: no identity, no hardened runtime (and so no entitlements to carry). --deep is
  # acceptable here precisely because there are no entitlements for it to get wrong; what it
  # buys is a resource seal over the whole bundle, nested code included.
  echo "    APPLE_SIGNING_IDENTITY unset — ad-hoc signing"
  codesign --force --deep --sign - "$STAGED"
fi
# The signature is the checkpoint: a bundle that fails this launches and then breaks in ways
# that only show up on someone else's Mac. Catch it here, not after installing.
# (No --verbose: with --deep it prints a line for every nested file.)
if ! codesign --verify --deep --strict "$STAGED"; then
  echo "ERROR: the app bundle signature is not valid" >&2
  exit 1
fi
echo "    signature valid"

echo "==> [4/5] update archive"
# Built from the signed, staged bundle — the same bytes that go in the .dmg — before the
# /Applications symlink joins it in the staging dir. COPYFILE_DISABLE keeps macOS tar from
# adding ._ AppleDouble files, which would land inside the installed bundle and break its seal.
UPDATE="$DIST/${APP}_${VERSION}_${ARCH}.app.tar.gz"
rm -f "$UPDATE" "$UPDATE.sig"
COPYFILE_DISABLE=1 /usr/bin/tar -C "$STAGING" -czf "$UPDATE" "$APP.app"
if [ -n "${TEMPO_UPDATE_PRIVATE_KEY:-}" ] || [ -f "$ROOT/secrets/tempo_update_key" ]; then
  "$PY" "$ROOT/scripts/sign_update.py" "$UPDATE"
elif [ "${TEMPO_REQUIRE_UPDATE_SIGNATURE:-}" = "1" ]; then
  echo "ERROR: TEMPO_REQUIRE_UPDATE_SIGNATURE=1 but no update signing key (set" >&2
  echo "       TEMPO_UPDATE_PRIVATE_KEY or create secrets/tempo_update_key)" >&2
  exit 1
else
  echo "    WARNING: no update signing key — $(basename "$UPDATE") is unsigned, and installed"
  echo "    copies will refuse it. Fine for a test build; releases must be signed."
fi

echo "==> [5/5] hdiutil: wrapping into .dmg"
# What the user drags onto. Standard install gesture, no Finder scripting.
ln -s /Applications "$STAGING/Applications"
# The window: a background at 1x and 2x in one TIFF, and the Finder layout that places the two
# icons on it. hdiutil builds the image straight from this directory and never mounts it, so
# nothing can lay the window out at build time; the layout Finder would have written is checked
# in instead (desktop/installer/dmg-layout.py says how to regenerate it).
INSTALLER="$ROOT/desktop/installer"
tiffutil -cathidpicheck "$INSTALLER/dmg-background.png" "$INSTALLER/dmg-background@2x.png" \
  -out "$STAGING/.background.tiff" >/dev/null
cp "$INSTALLER/dmg-DS_Store" "$STAGING/.DS_Store"
DMG="$DIST/${APP}_${VERSION}_${ARCH}.dmg"
rm -f "$DMG"
# Clear any stale mount so our image doesn't mount as "$APP 1".
[ -d "/Volumes/$APP" ] && hdiutil detach "/Volumes/$APP" -force >/dev/null 2>&1 || true
# The volume must stay named "Tempo": the layout finds .background.tiff by its path on it.
hdiutil create -volname "$APP" -srcfolder "$STAGING" -ov -format UDZO \
  -imagekey zlib-level=9 "$DMG" >/dev/null
hdiutil verify -quiet "$DMG"

if [ -z "${APPLE_SIGNING_IDENTITY:-}" ]; then
  echo ""
  echo "    AD-HOC signed build — it runs, but Gatekeeper doesn't know the signer, so the first"
  echo "    launch on each Mac needs System Settings → Privacy & Security → Open Anyway (or"
  echo "    right-click → Open on macOS 14 and earlier). Set APPLE_SIGNING_IDENTITY to sign"
  echo "    with a Developer ID instead."
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
echo "       $UPDATE"
[ -f "$UPDATE.sig" ] && echo "       $UPDATE.sig"
exit 0
