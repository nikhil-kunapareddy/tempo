"""In-app updates: find a newer Tempo on GitHub, fetch it in the background, swap it in on request.

The flow mirrors Gavia's, rebuilt in Python because there is no Tauri updater here any more:

  1. On launch (desktop/main.py calls `start_check()`), read `latest.json` from the release
     GitHub marks Latest. Pre-releases are never Latest, so a beta reaches nobody until it is
     promoted by hand.
  2. If it names a strictly newer version, download the `.app.tar.gz` it points at and check it
     twice: the SHA-256 from the manifest and an Ed25519 signature made with a key that lives
     only in CI. HTTPS alone isn't enough — anyone able to edit a release could otherwise ship
     code to every installed copy.
  3. Unpack it, confirm it is a `Tempo.app` of the advertised version with an intact code
     signature, and report `ready`. The UI then offers "Restart to update".
  4. Only when the user clicks it does `install()` hand off to a small shell script that waits
     for this process to exit, moves the running bundle aside, moves the new one into place
     and relaunches it. Tempo never restarts on its own.

Nothing here raises into the UI: no network, no stable release yet (GitHub answers 404) or a bad
download all end quietly in a state the Settings panel can describe.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from . import config

MANIFEST_URL = "https://github.com/nikhil-kunapareddy/tempo/releases/latest/download/latest.json"
PLATFORM = "darwin-aarch64"
APP_NAME = "Tempo.app"

# Ed25519, raw 32 bytes, base64. The private half is the TEMPO_UPDATE_PRIVATE_KEY CI secret.
PUBLIC_KEY = "Qs3xC9PL8ugqLUeeUTqPf04kyPT1GPlcCO5o/QmrBqo="

CHECK_TIMEOUT = 10.0
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024  # a corrupt or hostile server can't fill the disk
QUIT_DELAY = 0.5  # long enough for the install request's response to reach the webview

MOVE_TO_APPLICATIONS = "Move Tempo to your Applications folder to get updates."

_VERSION_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})


class UpdateError(Exception):
    """A failed check or install, with a message short enough to show in Settings."""


# -- versions ------------------------------------------------------------------------------
def version_key(version: str) -> tuple:
    """A sort key that orders versions the way semver does, pre-releases included.

    `0.2.0-beta.1 < 0.2.0-rc.1 < 0.2.0 < 0.10.0`. Numeric pre-release identifiers compare as
    numbers and sort before alphanumeric ones; build metadata (`+...`) is ignored.
    """
    match = _VERSION_RE.match(version.strip())
    if not match:
        raise ValueError(f"not a version: {version!r}")
    major, minor, patch, pre = match.groups()
    if pre is None:
        pre_key: tuple = (1,)  # a release outranks every pre-release of the same version
    else:
        pre_key = (0, *((0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")))
    return (int(major), int(minor), int(patch), pre_key)


def is_newer(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)


def current_version() -> str:
    from desktop import __version__

    return __version__


# -- the manifest --------------------------------------------------------------------------
@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    url: str
    signature: bytes
    sha256: str


def manifest_url() -> str:
    return os.environ.get("TEMPO_UPDATE_MANIFEST_URL", "").strip() or MANIFEST_URL


def _download_url_ok(url: str) -> bool:
    """HTTPS, or plain HTTP to this machine so a local test server can stand in for GitHub."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return bool(parts.hostname)
    return parts.scheme == "http" and (parts.hostname or "") in _LOCAL_HOSTS


def parse_manifest(data: Any) -> Release:
    """Validate a decoded latest.json and pull out this platform's entry."""
    if not isinstance(data, dict):
        raise UpdateError("The update manifest is malformed.")
    version = data.get("version")
    if not isinstance(version, str):
        raise UpdateError("The update manifest has no version.")
    try:
        version_key(version)
    except ValueError:
        raise UpdateError(f"The update manifest has an invalid version: {version}") from None
    platforms = data.get("platforms")
    entry = platforms.get(PLATFORM) if isinstance(platforms, dict) else None
    if not isinstance(entry, dict):
        raise UpdateError(f"The update manifest has no {PLATFORM} build.")
    url, signature, sha256 = entry.get("url"), entry.get("signature"), entry.get("sha256")
    if not isinstance(url, str) or not _download_url_ok(url):
        raise UpdateError("The update manifest has no usable download URL.")
    if not isinstance(sha256, str) or not _SHA256_RE.match(sha256):
        raise UpdateError("The update manifest has no valid SHA-256.")
    try:
        raw_signature = base64.b64decode(signature, validate=True) if isinstance(signature, str) else b""
    except ValueError:
        raw_signature = b""
    if len(raw_signature) != 64:
        raise UpdateError("The update manifest has no valid signature.")
    notes = data.get("notes")
    return Release(
        version=version.lstrip("v"),
        notes=notes if isinstance(notes, str) else "",
        url=url,
        signature=raw_signature,
        sha256=sha256.lower(),
    )


def fetch_manifest() -> Release | None:
    """The Latest release's manifest, or None when there is no stable release yet."""
    try:
        response = httpx.get(manifest_url(), timeout=CHECK_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError:
        raise UpdateError("Couldn't reach GitHub to check for updates.") from None
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise UpdateError(f"GitHub answered {response.status_code} to the update check.")
    try:
        data = response.json()
    except ValueError:
        raise UpdateError("The update manifest is not valid JSON.") from None
    return parse_manifest(data)


# -- download + verification ---------------------------------------------------------------
def updates_dir() -> Path:
    return config.STATE_DIR / "updates"


def previous_bundle() -> Path:
    """Where the replaced bundle is kept, so a bad update can be undone by hand."""
    return updates_dir() / "previous" / APP_NAME


def download(release: Release, dest: Path) -> Path:
    """Stream the archive to `dest`, via a .part file so a cut-off download never looks whole."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    timeout = httpx.Timeout(CHECK_TIMEOUT, read=60.0)
    try:
        with httpx.stream("GET", release.url, timeout=timeout, follow_redirects=True) as response:
            if response.status_code != 200:
                raise UpdateError(f"GitHub answered {response.status_code} to the download.")
            written = 0
            with open(partial, "wb") as handle:
                for chunk in response.iter_bytes():
                    written += len(chunk)
                    if written > MAX_ARCHIVE_BYTES:
                        raise UpdateError("The update download is unexpectedly large.")
                    handle.write(chunk)
    except httpx.HTTPError:
        partial.unlink(missing_ok=True)
        raise UpdateError("The update download was interrupted.") from None
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(dest)
    return dest


def verify_archive(archive: Path, release: Release) -> None:
    """Check the SHA-256 and the Ed25519 signature. A mismatch deletes the file."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        data = archive.read_bytes()
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), release.sha256):
            raise UpdateError("The update download is corrupt (checksum mismatch).")
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY))
        try:
            key.verify(release.signature, data)
        except InvalidSignature:
            raise UpdateError("The update's signature didn't match; it was discarded.") from None
    except BaseException:
        archive.unlink(missing_ok=True)
        raise


def _codesign_verify(bundle: Path) -> bool:
    result = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def stage_bundle(archive: Path, staging: Path, version: str) -> Path:
    """Unpack the archive into a fresh `staging` dir and check what came out of it."""
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    result = subprocess.run(
        ["/usr/bin/tar", "-xzf", str(archive), "-C", str(staging)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise UpdateError("The update couldn't be unpacked.")
    # AppleDouble `._*` files appear if the archive was made without COPYFILE_DISABLE; they
    # carry no code and are not part of the bundle.
    entries = sorted(p.name for p in staging.iterdir() if not p.name.startswith("._"))
    if entries != [APP_NAME]:
        raise UpdateError(f"The update doesn't contain {APP_NAME}.")
    bundle = staging / APP_NAME
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        raise UpdateError("The update's Info.plist is missing or unreadable.") from None
    if info.get("CFBundleShortVersionString") != version:
        raise UpdateError("The update's version doesn't match its manifest.")
    if not _codesign_verify(bundle):
        raise UpdateError("The update's code signature is broken.")
    return bundle


def _clear_stale(keep: str) -> None:
    """Drop downloads of other versions; `previous` is the rollback copy and stays."""
    root = updates_dir()
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.is_dir() and child.name not in (keep, "previous"):
            shutil.rmtree(child, ignore_errors=True)


# -- where the running app lives -----------------------------------------------------------
def _forced() -> bool:
    return os.environ.get("TEMPO_UPDATE_FORCE") == "1"


def running_bundle() -> Path | None:
    """The `.app` this process runs from, or None outside a bundle.

    Frozen: `Tempo.app/Contents/MacOS/Tempo`, two levels below the bundle. With
    TEMPO_UPDATE_FORCE=1, TEMPO_UPDATE_BUNDLE can name a bundle so a source run can be tested.
    """
    if _forced() and os.environ.get("TEMPO_UPDATE_BUNDLE"):
        return Path(os.environ["TEMPO_UPDATE_BUNDLE"])
    if not (getattr(sys, "frozen", False) or _forced()):
        return None
    candidate = Path(sys.executable).resolve().parents[2]
    return candidate if candidate.suffix == ".app" else None


def location_problem(bundle: Path) -> str | None:
    """Why a bundle at this path can't be replaced in place, or None if it can.

    Gatekeeper runs a quarantined app that was never moved from a randomized read-only copy
    (App Translocation), and a disk image is read-only; neither can be swapped.
    """
    path = str(bundle)
    if "/AppTranslocation/" in path or path.startswith("/Volumes/"):
        return MOVE_TO_APPLICATIONS
    if not os.access(bundle.parent, os.W_OK) or not os.access(bundle, os.W_OK):
        return f"Tempo can't replace itself in {bundle.parent}. {MOVE_TO_APPLICATIONS}"
    return None


# -- the swap ------------------------------------------------------------------------------
# Runs after Tempo has quit, so it can't be Python inside the bundle it is replacing.
_INSTALL_SCRIPT = r"""#!/bin/bash
# Swap a staged Tempo.app into place once the running copy has quit, then relaunch it.
#   install-update.sh <pid> <current bundle> <staged bundle> <previous bundle>
set -u
pid="$1"; current="$2"; staged="$3"; previous="$4"
open_cmd="${TEMPO_OPEN:-/usr/bin/open}"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

case "$current" in *.app) ;; *) log "refusing: $current is not an app bundle"; exit 2 ;; esac
case "$staged" in *.app) ;; *) log "refusing: $staged is not an app bundle"; exit 2 ;; esac

for _ in $(seq 1 300); do
  kill -0 "$pid" 2>/dev/null || break
  sleep 0.1
done
if kill -0 "$pid" 2>/dev/null; then
  log "Tempo (pid $pid) did not quit within 30s; leaving it as it is"
  exit 1
fi
if [ ! -d "$staged" ]; then
  log "no staged bundle at $staged"
  "$open_cmd" "$current"
  exit 1
fi

mkdir -p "$(dirname "$previous")"
rm -rf "$previous"
if ! mv "$current" "$previous"; then
  log "could not move $current aside"
  "$open_cmd" "$current"
  exit 1
fi
if ! mv "$staged" "$current"; then
  log "could not move the new version into place; restoring the previous one"
  rm -rf "$current"
  mv "$previous" "$current"
  "$open_cmd" "$current"
  exit 1
fi
log "updated $current"
"$open_cmd" "$current"
"""


def write_install_script() -> Path:
    path = updates_dir() / "install-update.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_INSTALL_SCRIPT)
    path.chmod(0o700)
    return path


def install_command(script: Path, pid: int, current: Path, staged: Path) -> list[str]:
    return ["/bin/bash", str(script), str(pid), str(current), str(staged), str(previous_bundle())]


# -- state ---------------------------------------------------------------------------------
class Updater:
    """One check per launch, and the state the UI polls. Safe to call from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "idle"
        self._version: str | None = None
        self._notes: str | None = None
        self._message: str | None = None
        self._staged: Path | None = None
        self._installing = False
        self._thread: threading.Thread | None = None
        self._quit: Callable[[], None] | None = None

    def set_quit_handler(self, handler: Callable[[], None] | None) -> None:
        """How `install()` ends the app. desktop/main.py registers one; headless has none."""
        self._quit = handler

    def _set(self, state: str, *, version=None, notes=None, message=None) -> None:
        with self._lock:
            self._state, self._version, self._notes, self._message = state, version, notes, message

    def _availability(self) -> tuple[str, str | None]:
        """("idle", None) when a check could run now, else the state that explains why not."""
        bundle = running_bundle()
        if bundle is None:
            return "unsupported", "Updates are only available in the packaged Tempo app."
        problem = location_problem(bundle)
        if problem:
            return "unsupported", problem
        if not config.auto_update_enabled():
            return "disabled", None
        return "idle", None

    def status(self) -> dict[str, Any]:
        with self._lock:
            state, version, notes, message = self._state, self._version, self._notes, self._message
        if state == "idle":
            state, message = self._availability()
        out: dict[str, Any] = {"state": state, "current_version": current_version()}
        if version:
            out["version"] = version
        if notes:
            out["notes"] = notes
        if message:
            out["message"] = message
        return out

    def start_check(self) -> bool:
        """Begin a background check, unless one already ran or updates are off/unsupported."""
        state, message = self._availability()
        with self._lock:
            if self._thread is not None:
                return False
            if state != "idle":
                self._state, self._message = state, message
                return False
            self._state = "checking"
            self._thread = threading.Thread(target=self.check, name="tempo-update", daemon=True)
            self._thread.start()
        return True

    def check(self) -> None:
        """Check, download, verify and stage. Never raises; the outcome lands in the state."""
        self._set("checking")
        try:
            release = fetch_manifest()
            if release is None or not is_newer(release.version, current_version()):
                self._set("up_to_date")
                return
            self._set("downloading", version=release.version)
            _clear_stale(keep=release.version)
            folder = updates_dir() / release.version
            archive = folder / "Tempo.app.tar.gz"
            if archive.exists():
                try:
                    verify_archive(archive, release)  # a finished download from an earlier launch
                except UpdateError:
                    download(release, archive)  # verify_archive already deleted the bad copy
                    verify_archive(archive, release)
            else:
                download(release, archive)
                verify_archive(archive, release)
            staged = stage_bundle(archive, folder / "staged", release.version)
            with self._lock:
                self._staged = staged
            self._set("ready", version=release.version, notes=release.notes or None)
            print(f"[tempo] update {release.version} is ready to install", flush=True)
        except UpdateError as exc:
            print(f"[tempo] update check failed: {exc}", file=sys.stderr, flush=True)
            self._set("error", message=str(exc))
        except Exception as exc:  # never let a bug here take the app down with it
            print(f"[tempo] update check crashed: {exc!r}", file=sys.stderr, flush=True)
            self._set("error", message="The update check failed unexpectedly.")

    def install(self) -> None:
        """Hand the staged bundle to the swap script, then quit so it can run."""
        with self._lock:
            if self._state != "ready" or self._staged is None:
                raise UpdateError("No update is ready to install.")
            if self._installing:
                raise UpdateError("The update is already being installed.")
            staged = self._staged
        if self._quit is None:
            raise UpdateError("Quit and reopen Tempo to install the update.")
        bundle = running_bundle()
        if bundle is None:
            raise UpdateError("Updates are only available in the packaged Tempo app.")
        problem = location_problem(bundle)
        if problem:
            raise UpdateError(problem)
        if not staged.is_dir():
            raise UpdateError("The downloaded update has gone missing; restart Tempo to fetch it again.")

        script = write_install_script()
        log_dir = config.STATE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "update-install.log", "a", encoding="utf-8") as log:
            subprocess.Popen(
                install_command(script, os.getpid(), bundle, staged),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # outlives Tempo, which it is waiting on
                close_fds=True,
            )
        with self._lock:
            self._installing = True
            self._message = "Restarting Tempo…"
        threading.Timer(QUIT_DELAY, self._quit).start()


_updater = Updater()
status = _updater.status
start_check = _updater.start_check
install = _updater.install
set_quit_handler = _updater.set_quit_handler
