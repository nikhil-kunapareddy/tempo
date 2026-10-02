#!/usr/bin/env python3
"""Tag a release and create it on GitHub; CI builds the .dmg and attaches it.

    python scripts/release.py                  next beta of the current version, e.g. v0.2.0-beta.3
    python scripts/release.py rc               next release candidate
    python scripts/release.py stable           v0.2.0 itself
    python scripts/release.py --dry            print the tag and stop
    python scripts/release.py --check-tag TAG  exit 0 if TAG matches the version (CI runs this)

The version comes from `__version__` in desktop/__init__.py; bump it there (and add a
CHANGELOG entry) in a PR before releasing a new one.

The release is created here, with your own `gh` login, rather than by CI: GitHub will not let
a workflow's own token create a release that then triggers other workflows, but it can upload
to one that already exists. Every release starts as a pre-release so "Latest" never points at
a release whose .dmg is still building; promote a stable one by hand once CI has attached it.
Promoting is also what ships it to installed copies, which read latest.json from the release
marked Latest.

Standard library only: CI runs --check-tag before any dependencies are installed.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

REPO = "nikhil-kunapareddy/tempo"
CHANNELS = ("beta", "rc", "stable")
ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "desktop" / "__init__.py"

# v0.2.0, v0.2.0-beta.3, v0.2.0-rc.1 — nothing else is a release tag.
TAG_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)(?:-(?P<channel>beta|rc)\.(?P<number>[1-9]\d*))?$")


class ReleaseError(Exception):
    pass


def read_version(path: Path = VERSION_FILE) -> str:
    """`__version__` from desktop/__init__.py, parsed rather than imported.

    Importing would run the package, and this has to work on a CI runner before the app's
    dependencies are installed.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if not re.fullmatch(r"\d+\.\d+\.\d+", value.value):
                    raise ReleaseError(f"__version__ {value.value!r} in {path} is not X.Y.Z")
                return value.value
    raise ReleaseError(f"no __version__ = '...' in {path}")


def check_tag(tag: str, version: str) -> None:
    """Raise unless `tag` is a release tag for `version`."""
    match = TAG_RE.match(tag)
    if not match:
        raise ReleaseError(f"{tag!r} is not a release tag (vX.Y.Z, vX.Y.Z-beta.N or vX.Y.Z-rc.N)")
    if match["version"] != version:
        raise ReleaseError(
            f"tag {tag} is for {match['version']}, but desktop/__init__.py says {version}"
        )


def next_tag(version: str, channel: str, existing: list[str]) -> str:
    """The tag to create: v<version> for stable, else the next free -<channel>.N."""
    if channel not in CHANNELS:
        raise ReleaseError(f"unknown channel {channel!r}; use one of {', '.join(CHANNELS)}")
    if channel == "stable":
        tag = f"v{version}"
    else:
        prefix = f"v{version}-{channel}."
        taken = [int(t[len(prefix):]) for t in existing if t.startswith(prefix) and t[len(prefix):].isdigit()]
        tag = f"{prefix}{max(taken) + 1 if taken else 1}"
    if tag in existing:
        raise ReleaseError(f"{tag} already exists")
    return tag


def _run(*argv: str, show: bool = False) -> str:
    result = subprocess.run(
        argv,
        cwd=ROOT,
        text=True,
        stdout=None if show else subprocess.PIPE,
        stderr=None if show else subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() if not show else ""
        raise ReleaseError(f"`{' '.join(argv)}` failed" + (f": {detail}" if detail else ""))
    return (result.stdout or "").strip()


def _tags() -> list[str]:
    return [t for t in _run("git", "tag", "--list", "v*").splitlines() if t]


def _preflight() -> None:
    try:
        _run("gh", "auth", "status")
    except (ReleaseError, FileNotFoundError):
        raise ReleaseError("the GitHub CLI is not signed in; run `gh auth login` first") from None
    if _run("git", "status", "--porcelain"):
        raise ReleaseError("the working tree has uncommitted changes")
    _run("git", "fetch", "--tags", "--quiet")
    try:
        unpushed = _run("git", "log", "@{u}..HEAD", "--oneline")
    except ReleaseError:
        raise ReleaseError("this branch has no upstream; push it first") from None
    if unpushed:
        raise ReleaseError("this branch has commits that are not pushed yet")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("channel", nargs="?", default="beta", choices=CHANNELS)
    parser.add_argument("--dry", action="store_true", help="print the tag and stop")
    parser.add_argument("--check-tag", metavar="TAG", help="validate TAG against the version")
    args = parser.parse_args(argv)

    try:
        version = read_version()
        if args.check_tag is not None:
            check_tag(args.check_tag, version)
            print(f"{args.check_tag} matches version {version}")
            return 0

        if not args.dry:
            _preflight()
        tag = next_tag(version, args.channel, _tags())
        if args.dry:
            print(tag)
            return 0

        if args.channel == "stable":
            notes = f"Tempo {tag}. Download the .dmg below; see the README for install steps."
        else:
            notes = (
                f"A {args.channel} build of Tempo {version}, for testing. It may have rough "
                "edges — please report anything that goes wrong."
            )
        _run("git", "tag", "-a", tag, "-m", f"Tempo {tag}", show=True)
        _run("git", "push", "origin", tag, show=True)
        _run(
            "gh", "release", "create", tag, "--repo", REPO, "--verify-tag", "--prerelease",
            "--title", f"Tempo {tag}", "--notes", notes,
            show=True,
        )
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1

    print(f"\nCreated {tag}. CI is building the .dmg: https://github.com/{REPO}/actions")
    if args.channel == "stable":
        print(
            "Once it and latest.json are attached, promote it; installed copies then offer the update:\n"
            f"  gh release edit {tag} --repo {REPO} --prerelease=false --latest"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
