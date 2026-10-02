"""PyInstaller entry point for the packaged Tempo.app.

PyInstaller needs a concrete top-level script to analyze, and the analyzed script runs as
`__main__` rather than as a package member — so it can't be `desktop/main.py` itself, whose
`from . import shell` is a relative import. This thin wrapper imports it the normal way.

Run from source with `python -m desktop.main` instead; this file is only for the frozen build.
"""

from desktop.main import main

if __name__ == "__main__":
    raise SystemExit(main())
