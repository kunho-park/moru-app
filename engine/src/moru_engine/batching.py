"""Shared prompt-batch packing — single source of truth for batch limits.

Both the runtime orchestrator and the evalset builder pack entries with
``pack_batches``, so evaluation batches can never drift from production
batching behavior.

``dedup_entries``/``expand_aliases`` bracket that packing: a modpack
repeats the same short string ("Health", "Right-click to open") across
mods and files, and every repeat is a paid model call. Collapsing them
onto one dispatched entry and handing that translation back to every
occurrence is free quality-wise ONLY under the scoping rule documented on
``dedup_entries``; read it before widening the scope.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

DEFAULT_BATCH_SIZE = 30
DEFAULT_MAX_BATCH_CHARS = 8000

#: A key whose tail is an ordinal: ``...tooltip2``, ``...desc.3``,
#: ``...description[4]``. The stem keeps the separator, so ``tooltip2``
#: and ``tooltip.2`` never join the same run.
_NUMBERED_KEY_RE = re.compile(r"^(?P<stem>.*?)(?:\[(?P<braced>\d+)\]|(?P<bare>\d+))$")


def continuation_groups(keys: Iterable[str]) -> list[list[str]]:
    """Numbered sibling runs, each ordered by its trailing ordinal.

    Mods implement multi-line tooltips and descriptions as consecutive
    numbered keys (``...tooltip2``, ``...tooltip3``), and one sentence
    routinely continues across them. Such a run has to reach the model
    whole and in order, or weaker models scatter its clauses between the
    lines. Only runs of two or more keys are returned; every other key
    stands alone and belongs to no group.
    """
    runs: dict[str, list[tuple[int, str]]] = {}
    for key in keys:
        match = _NUMBERED_KEY_RE.match(key)
        if match is None:
            continue
        ordinal = match["braced"] if match["braced"] is not None else match["bare"]
        runs.setdefault(match["stem"], []).append((int(ordinal), key))
    return [
        [key for _, key in sorted(members)]
        for members in runs.values()
        if len(members) > 1
    ]


def dedup_entries(entries: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Collapse repeated texts onto one representative key.

    Returns ``(unique, aliases)``: ``unique`` is the subset to dispatch,
    in the input's own order so packing is unchanged, and ``aliases`` maps
    every dropped key to the key whose result it must reuse.

    Scoping rule — two occurrences may share one translation only when
    BOTH of the following hold, and this function enforces both:

    1. **Identical text.** Keying on the value the caller is about to
       dispatch is what makes the two model inputs interchangeable. The
       pipeline dispatches PROTECTED text, and that is deliberately the
       stronger key, not merely the convenient one: protection erases
       differences that cannot change the words. "§6Gold" and "§aGold"
       both protect to "{{COLOR}}Gold", so they share a call, and each
       key's own ``ProtectedText`` restores its own literal afterwards.
    2. **Identical context.** The prompt carries per-batch context (file,
       handler, sibling lines) and a glossary filtered from the batch's
       own texts, so the same English string can legitimately need
       different translations in different contexts. One call to this
       function must therefore cover exactly one context — for the
       pipeline, one file's translate wave, whose context string is fixed
       before packing. Merging occurrences from two files would silently
       trade quality for cost; that is worse than paying twice.

    Keys inside a continuation group (``continuation_groups``) are never
    merged, in either direction. A numbered sibling run is read as ONE
    sentence spread over several display lines, so line 2 of run A
    continues a different clause than the identical line 2 of run B —
    precisely the case where context decides the translation. Excluding
    them also keeps every run whole for ``pack_batches``, which relies on
    seeing all of a run's members.
    """
    grouped = {key for run in continuation_groups(entries) for key in run}
    unique: dict[str, str] = {}
    representative: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for key, text in entries.items():
        if key in grouped:
            unique[key] = text
            continue
        first = representative.get(text)
        if first is None:
            representative[text] = key
            unique[key] = text
        else:
            aliases[key] = first
    return unique, aliases


def expand_aliases[ResultT](
    results: Mapping[str, ResultT],
    aliases: Mapping[str, str],
) -> dict[str, ResultT]:
    """Give every alias key the result its representative received.

    Applies to any per-key mapping the dispatch produced — the batch
    itself, translations, failure reasons — so the caller's downstream
    loop sees the full key set again and treats a merged occurrence
    exactly like a dispatched one. An alias whose representative is
    absent stays absent, so a failed representative fails all of its
    occurrences instead of silently dropping them.
    """
    expanded = dict(results)
    for alias, source in aliases.items():
        if source in results:
            expanded[alias] = results[source]
    return expanded


def _chunk_run(
    run: list[str],
    entries: Mapping[str, str],
    batch_size: int,
    max_batch_chars: int,
) -> list[tuple[list[str], int]]:
    """Split one continuation run into ``(keys, chars)`` packing units.

    A run that fits the limits yields exactly one unit and therefore
    never straddles a batch seam. A run too big for one batch has to
    give: chunk it in key order so at most one seam falls inside the run
    instead of scattering its lines across batches.
    """
    chunks: list[tuple[list[str], int]] = []
    current: list[str] = []
    current_chars = 0
    for key in run:
        text_chars = len(entries[key])
        if current and (
            len(current) >= batch_size
            or current_chars + text_chars > max_batch_chars
        ):
            chunks.append((current, current_chars))
            current = []
            current_chars = 0
        current.append(key)
        current_chars += text_chars
    if current:
        chunks.append((current, current_chars))
    return chunks


def pack_batches(
    entries: Mapping[str, str],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batch_chars: int = DEFAULT_MAX_BATCH_CHARS,
) -> list[dict[str, str]]:
    """Greedy in-order packing that keeps continuation runs whole.

    The packing unit is one entry, or one continuation run (numbered
    sibling keys that may carry a single sentence across several display
    lines) which is emitted at the position of its first member, in key
    order, and never split. A batch is closed when it already holds
    ``batch_size`` entries or the next unit would push its character sum
    past ``max_batch_chars``. A single oversized unit still forms its own
    batch. Every input key appears in exactly one returned batch.
    """
    run_of: dict[str, list[str]] = {}
    for run in continuation_groups(entries):
        for key in run:
            run_of[key] = run

    units: list[tuple[list[str], int]] = []
    packed: set[str] = set()
    for key, text in entries.items():
        if key in packed:
            continue
        run = run_of.get(key)
        if run is None:
            units.append(([key], len(text)))
            continue
        packed.update(run)
        units.extend(_chunk_run(run, entries, batch_size, max_batch_chars))

    batches: list[dict[str, str]] = []
    current: dict[str, str] = {}
    current_chars = 0
    for unit_keys, unit_chars in units:
        if current and (
            len(current) + len(unit_keys) > batch_size
            or current_chars + unit_chars > max_batch_chars
        ):
            batches.append(current)
            current = {}
            current_chars = 0
        for key in unit_keys:
            current[key] = entries[key]
        current_chars += unit_chars
    if current:
        batches.append(current)
    return batches
