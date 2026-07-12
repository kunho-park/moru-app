"""PyInstaller entrypoint for the frozen engine sidecar.

Kept outside the package so the spec has a stable, import-free script to
analyze. Mirrors ``python -m moru_engine.server``.
"""

import multiprocessing
import sys

from moru_engine.server.__main__ import main

if __name__ == "__main__":
    # Required on Windows so a frozen child re-exec doesn't re-run the server.
    multiprocessing.freeze_support()
    sys.exit(main())
