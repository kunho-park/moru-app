from __future__ import annotations

from collections.abc import Callable

type ScanProgressCallback = Callable[[str, int, int, str], None]
