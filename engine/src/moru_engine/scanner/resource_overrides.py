"""Enabled resource-pack overrides as the effective source text.

Modpack authors routinely rename items with a resource pack bundled in
the pack instead of editing the mod: ``archers_expansion`` ships
``"item.archers_expansion.deadeye_spell_book": "Bounty List"`` while the
player of that pack reads ``"Alnilam's Sight"``. Translating the mod's
own string produces a translation of text that is not in the game.

This module reads the instance's ``options.txt``, resolves the resource
packs it enables and returns their source-locale language entries, so
the scanner can substitute them into the source text it hands the
pipeline. Only *values* are substituted: the key set and the file the
key came from stay exactly as scanned, which leaves output routing and
generation untouched.

Everything here is failure-tolerant, and every failure means "no
override", which reproduces the pre-override behaviour exactly: no
``options.txt``, an unreadable or option-less one, a pack listed but
missing from disk, a built-in entry like ``"vanilla"``, and a pack with
no language files all contribute nothing.
"""

from __future__ import annotations

import json
import logging
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..parsers import BaseParser, JSONParser, LangParser, ParserError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

logger = logging.getLogger(__name__)

OPTIONS_FILE = "options.txt"

#: ``(options.txt directory, resourcepacks root)`` relative to the scanned
#: path, in probe order. The game writes ``options.txt`` into its game
#: directory next to ``mods/`` and ``resourcepacks/``, which is normally
#: the path the scanner is handed; a Prism/MultiMC user may instead select
#: the instance root holding the game dir (mirroring
#: :mod:`~moru_engine.scanner.pack_identity`'s probing).
#:
#: The last two entries are the pack author's *declared* selection, shipped
#: by the ConfiguredDefaults and Default Options mods and copied to
#: ``options.txt`` on first launch — the only enabled-pack list a freshly
#: installed pack has, since the real one does not exist until the game
#: has run once. Their packs still live in the game dir's
#: ``resourcepacks/``, not beside the defaults file.
_OPTIONS_CANDIDATES: tuple[tuple[str, str], ...] = (
    (".", "."),
    (".minecraft", ".minecraft"),
    ("minecraft", "minecraft"),
    ("configureddefaults", "."),
    ("config/defaultoptions", "."),
)

#: The enabled-pack line. Matched as a whole prefix so the neighbouring
#: ``incompatibleResourcePacks:`` line cannot be mistaken for it.
_RESOURCE_PACKS_PREFIX = "resourcePacks:"

#: Modern (1.13+) marker for "an entry under resourcepacks/"; older
#: versions wrote the bare file name. Built-ins ("vanilla", "fabric",
#: "mod_resources", "programer_art") carry no prefix and resolve to no
#: file, so both formats are handled by trying the name as written.
_PACK_FILE_PREFIX = "file/"

#: Language-file suffix -> path-free reader. A pack keeps them at
#: ``assets/<namespace>/lang/<locale>.<suffix>``; the readers are the
#: pipeline's own parsers, so a pack's file is understood exactly the way
#: the same file is understood once scanned.
_LANG_READERS: dict[str, Callable[[str], dict[str, str]]] = {
    ".json": JSONParser.parse_text,
    ".lang": LangParser.parse_text,
}


@dataclass(frozen=True, slots=True)
class OverrideEntry:
    """One source string an enabled resource pack replaces."""

    value: str
    #: Pack file/directory name, for attribution in the scan report.
    pack: str


@dataclass(slots=True)
class InstanceOptions:
    """Located ``options.txt`` and the pack root its entries resolve in."""

    options_path: Path
    resourcepacks_root: Path


def find_instance_options(modpack_path: Path) -> InstanceOptions | None:
    """First ``options.txt`` among the probed locations, or None."""
    for options_dir, pack_dir in _OPTIONS_CANDIDATES:
        options_path = modpack_path / options_dir / OPTIONS_FILE
        if options_path.is_file():
            return InstanceOptions(
                options_path=options_path,
                resourcepacks_root=modpack_path / pack_dir / "resourcepacks",
            )
    return None


def parse_enabled_packs(text: str) -> list[str]:
    """Pack names from the ``resourcePacks:`` line, lowest priority first.

    The line holds a JSON array in load order — ``"vanilla"`` leads it
    because vanilla assets are the layer everything else is applied over,
    so later entries win. Anything unparseable yields no packs.
    """
    for line in text.splitlines():
        if not line.startswith(_RESOURCE_PACKS_PREFIX):
            continue
        payload = line[len(_RESOURCE_PACKS_PREFIX) :].strip()
        try:
            entries = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("Unparseable resourcePacks line: %s", payload[:120])
            return []
        return [entry for entry in entries if isinstance(entry, str) and entry]
    return []


def resolve_pack(resourcepacks_root: Path, name: str) -> Path | None:
    """Path of one enabled entry, or None when there is no file for it.

    None covers both cases that must not disturb the scan: a built-in
    layer such as ``"vanilla"``/``"fabric"``, which has no file to read,
    and a pack still listed in ``options.txt`` after the user deleted it.
    """
    relative = name[len(_PACK_FILE_PREFIX) :] if name.startswith(_PACK_FILE_PREFIX) else name
    if not relative or "/" in relative or "\\" in relative or relative.startswith("."):
        return None  # built-in layer, or an entry trying to escape the root
    candidate = resourcepacks_root / relative
    if candidate.is_file() or candidate.is_dir():
        return candidate
    logger.info("Enabled resource pack is not on disk, ignoring: %s", name)
    return None


def _lang_members(names: list[str], source_locale: str) -> Iterator[tuple[str, str]]:
    """``(namespace, member)`` for every ``assets/<ns>/lang/<locale>.*``."""
    for member in names:
        parts = member.replace("\\", "/").split("/")
        if len(parts) != 4 or parts[0] != "assets" or parts[2] != "lang":
            continue
        stem, _, suffix = parts[3].rpartition(".")
        if stem.lower() != source_locale or f".{suffix.lower()}" not in _LANG_READERS:
            continue
        yield parts[1], member


def read_pack_entries(pack: Path, source_locale: str) -> dict[str, dict[str, str]]:
    """``{namespace: {key: value}}`` from one pack, zipped or unpacked."""
    entries: dict[str, dict[str, str]] = {}

    def absorb(namespace: str, member: str, text: str) -> None:
        try:
            parsed = _LANG_READERS[Path(member).suffix.lower()](text)
        except (ValueError, ParserError) as exc:
            logger.warning("Unreadable language file %s in %s: %s", member, pack, exc)
            return
        if parsed:
            entries.setdefault(namespace, {}).update(parsed)

    try:
        if pack.is_dir():
            members = [
                str(path.relative_to(pack)) for path in pack.rglob("*") if path.is_file()
            ]
            for namespace, member in _lang_members(members, source_locale):
                absorb(
                    namespace,
                    member,
                    (pack / member).read_text(encoding="utf-8", errors="replace"),
                )
        else:
            with zipfile.ZipFile(pack) as zf:
                for namespace, member in _lang_members(zf.namelist(), source_locale):
                    absorb(
                        namespace,
                        member,
                        zf.read(member).decode("utf-8", errors="replace"),
                    )
    except (OSError, zipfile.BadZipFile) as exc:
        logger.warning("Unreadable resource pack %s: %s", pack, exc)

    return entries


def collect_overrides(
    modpack_path: Path, source_locale: str
) -> tuple[dict[str, dict[str, OverrideEntry]], list[Path]]:
    """Effective source strings from the instance's enabled packs.

    Returns ``({namespace: {key: OverrideEntry}}, packs_read)``. Packs are
    merged in ``options.txt`` order, so the highest-priority pack that
    defines a key is the one recorded against it.
    """
    located = find_instance_options(modpack_path)
    if located is None:
        logger.debug("No %s under %s; no source overrides", OPTIONS_FILE, modpack_path)
        return {}, []

    try:
        text = located.options_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not read %s: %s", located.options_path, exc)
        return {}, []

    overrides: dict[str, dict[str, OverrideEntry]] = {}
    packs_read: list[Path] = []
    for name in parse_enabled_packs(text):
        pack = resolve_pack(located.resourcepacks_root, name)
        if pack is None:
            continue
        entries = read_pack_entries(pack, source_locale)
        if not entries:
            continue
        packs_read.append(pack)
        for namespace, mapping in entries.items():
            bucket = overrides.setdefault(namespace, {})
            for key, value in mapping.items():
                bucket[key] = OverrideEntry(value=value, pack=pack.name)

    if packs_read:
        logger.info(
            "Enabled resource packs providing source overrides (%s): %s",
            located.options_path,
            ", ".join(pack.name for pack in packs_read),
        )
    return overrides, packs_read


async def apply_overrides(
    path: Path, entries: Mapping[str, OverrideEntry]
) -> Counter[str]:
    """Rewrite ``path``'s overridden values in place; tally them per pack.

    Only values of keys the file already carries are replaced, through
    the file's own parser, so the key set and the on-disk format survive.
    """
    parser = BaseParser.create_parser(path)
    if parser is None:
        return Counter()

    try:
        parsed = dict(await parser.parse())
    except (ParserError, OSError, ValueError) as exc:
        logger.warning("Could not read source file for overrides %s: %s", path, exc)
        return Counter()

    applied: Counter[str] = Counter()
    patched = dict(parsed)
    for key, current in parsed.items():
        entry = entries.get(key)
        if entry is None or entry.value == current:
            continue
        patched[key] = entry.value
        applied[entry.pack] += 1

    if not applied:
        return Counter()

    try:
        await parser.dump(patched)
    except (ParserError, OSError, ValueError) as exc:
        logger.warning("Could not apply overrides to %s: %s", path, exc)
        return Counter()

    logger.info("Applied %d resource-pack source overrides to %s", sum(applied.values()), path)
    return applied
