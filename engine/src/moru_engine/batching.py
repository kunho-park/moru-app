"""Shared prompt-batch packing — single source of truth for batch limits.

Both the runtime orchestrator and the evalset builder pack entries with
this function, so evaluation batches can never drift from production
batching behavior.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_BATCH_SIZE = 30
DEFAULT_MAX_BATCH_CHARS = 8000


def pack_batches(
    entries: Mapping[str, str],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batch_chars: int = DEFAULT_MAX_BATCH_CHARS,
) -> list[dict[str, str]]:
    """Greedy in-order packing.

    A batch is closed when it already holds ``batch_size`` entries or the
    next text would push its character sum past ``max_batch_chars``. A
    single oversized text still forms its own batch.
    """
    batches: list[dict[str, str]] = []
    current: dict[str, str] = {}
    current_chars = 0
    for key, text in entries.items():
        if current and (
            len(current) >= batch_size
            or current_chars + len(text) > max_batch_chars
        ):
            batches.append(current)
            current = {}
            current_chars = 0
        current[key] = text
        current_chars += len(text)
    if current:
        batches.append(current)
    return batches
