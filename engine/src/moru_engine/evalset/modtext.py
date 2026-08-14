"""Modpack gold-pair harvester (source of the "modtext" stratum).

A modpack that ships human-maintained source AND target locale files is
sentence-level gold: quest prose, NPC dialogue, tooltips, and Patchouli
guidebook pages written in the exact register the engine must produce —
the distribution the vanilla strata (short UI strings) never cover.
This module extracts those pairs from a pack zip or directory into a
deterministic goldset payload consumed by evalset.builder.

Sources scanned:
- ``**/assets/<ns>/lang/<locale>.json`` pairs (kubejs overrides,
  resource packs). ``ns == "minecraft"`` is EXCLUDED: those keys belong
  to the vanilla strata and sharing them here would let one key cross
  split assignments between strata.
- ``**/patchouli_books/<book>/<locale>/**.json`` parallel page files
  (``name``/``pages[].title``/``pages[].text``).

Filter policy (safety over coverage, like glossary.pair_harvester):
- the target must differ from the source after formatting cleanup
  (is_untranslated_copy) and contain target-script text when the target
  language has a known script;
- every format literal recognized by PlaceholderProtector must occur
  the SAME number of times on both sides, so builder._protect_pair
  mirrors the gold cleanly into {{KIND}} tokens. Translators adding or
  dropping markup is legitimate style, but such pairs cannot serve as
  token-faithful gold;
- the length ratio must sit inside the validator band [0.2, 3.0]
  (translator additions/omissions beyond that are edits, not
  translations);
- exact (source, target) duplicates are dropped after the first.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from ..glossary.pair_harvester import is_untranslated_copy
from ..placeholder import PlaceholderProtector

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

logger = logging.getLogger(__name__)

__all__ = ["MODTEXT_FORMAT", "harvest_pack", "load_goldset"]

MODTEXT_FORMAT = "moru-modtext/1"

MAX_LENGTH_RATIO = 3.0
MIN_LENGTH_RATIO = 0.2

_LANG_FILE_RE = re.compile(r"(?:^|/)assets/([^/]+)/lang/([a-z]{2}_[a-z]{2})\.json$")
_PATCHOULI_RE = re.compile(
    r"(?:^|/)patchouli_books/([^/]+)/([a-z]{2}_[a-z]{2})/(.+)\.json$"
)

#: Asset namespaces whose lang files are a known content type; everything
#: else is generic mod lang ("lang").
_NAMESPACE_CATEGORY = {
    "ftbquestlocalizer": "quest",
    "dialog": "dialog",
}

#: Target-script requirement per language prefix; languages without an
#: entry fall back to the copy check alone.
_SCRIPT_BY_LANG_PREFIX = {
    "ko": re.compile(r"[\uac00-\ud7a3]"),
    "ja": re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]"),
    "zh": re.compile(r"[\u4e00-\u9fff]"),
}


def _iter_json_members(root: Path) -> Iterator[tuple[str, bytes]]:
    """Yield (posix relpath, bytes) for every .json member of a zip/dir."""
    if root.is_dir():
        for path in sorted(root.rglob("*.json")):
            if path.is_file():
                yield path.relative_to(root).as_posix(), path.read_bytes()
        return
    with zipfile.ZipFile(root) as zf:
        for name in sorted(zf.namelist()):
            if name.endswith(".json"):
                yield name, zf.read(name)


def _decode(raw: bytes, member: str) -> object | None:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Skipping unparseable %s: %s", member, exc)
        return None


def _string_map(data: object) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    return {
        k: v
        for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str) and v.strip()
    }


def _pack_name(root: Path, members: Mapping[str, bytes]) -> str:
    for member in ("manifest.json", "overrides/manifest.json"):
        raw = members.get(member)
        if raw is None:
            continue
        data = _decode(raw, member)
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            version = data.get("version")
            suffix = f" {version}" if isinstance(version, str) and version else ""
            return f"{data['name']}{suffix}"
    return root.stem


class _PairFilter:
    """Stateful keep/drop filter; every drop reason is counted."""

    def __init__(self, target_lang: str) -> None:
        self._protector = PlaceholderProtector()
        self._script = _SCRIPT_BY_LANG_PREFIX.get(target_lang.split("_")[0])
        self._seen: set[tuple[str, str]] = set()
        self.dropped: Counter[str] = Counter()

    def _format_literals(self, text: str) -> Counter[str]:
        protected = self._protector.protect(text)
        return Counter(info.original for info in protected.placeholders)

    def keep(self, source: str, target: str) -> bool:
        source, target = source.strip(), target.strip()
        if len(source) < 2:
            return self._drop("source_too_short")
        protected = self._protector.protect(source)
        if self._protector.is_only_placeholders(protected):
            return self._drop("source_only_placeholders")
        if is_untranslated_copy(source, target):
            return self._drop("untranslated_copy")
        if self._script is not None and not self._script.search(target):
            return self._drop("missing_target_script")
        ratio = len(target) / len(source)
        if not (MIN_LENGTH_RATIO <= ratio <= MAX_LENGTH_RATIO):
            return self._drop("length_ratio")
        if self._format_literals(source) != self._format_literals(target):
            return self._drop("format_literal_mismatch")
        if (source, target) in self._seen:
            return self._drop("duplicate_pair")
        self._seen.add((source, target))
        return True

    def _drop(self, reason: str) -> bool:
        self.dropped[reason] += 1
        return False


def _harvest_lang_pairs(
    members: Mapping[str, bytes],
    source_lang: str,
    target_lang: str,
    keep: _PairFilter,
) -> list[dict[str, str]]:
    """Pairs from assets/<ns>/lang/ dirs shipping both locales."""
    by_dir: dict[str, dict[str, str]] = {}
    for member in members:
        match = _LANG_FILE_RE.search(member)
        if match:
            by_dir.setdefault(member[: match.start(2)], {})[match.group(2)] = member

    pairs: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    for prefix, locales in sorted(by_dir.items()):
        if source_lang not in locales or target_lang not in locales:
            continue
        namespace = _LANG_FILE_RE.search(locales[source_lang]).group(1)  # type: ignore[union-attr]
        if namespace == "minecraft":
            continue
        source_member, target_member = locales[source_lang], locales[target_lang]
        source_map = _string_map(_decode(members[source_member], source_member))
        target_map = _string_map(_decode(members[target_member], target_member))
        category = _NAMESPACE_CATEGORY.get(namespace, "lang")
        for key in sorted(source_map.keys() & target_map.keys()):
            pair_key = f"{namespace}:{key}"
            if pair_key in seen_keys:
                keep.dropped["duplicate_key"] += 1
                continue
            if keep.keep(source_map[key], target_map[key]):
                seen_keys.add(pair_key)
                pairs.append(
                    {
                        "key": pair_key,
                        "category": category,
                        "source": source_map[key].strip(),
                        "target": target_map[key].strip(),
                    }
                )
    return pairs


def _page_texts(data: object) -> list[tuple[str, str]]:
    """(field id, text) pairs of the translatable fields of one book file."""
    if not isinstance(data, dict):
        return []
    fields: list[tuple[str, str]] = []
    if isinstance(data.get("name"), str) and data["name"].strip():
        fields.append(("name", data["name"]))
    pages = data.get("pages")
    if isinstance(pages, list):
        for i, page in enumerate(pages):
            if not isinstance(page, dict):
                continue
            for attr in ("title", "text"):
                value = page.get(attr)
                if isinstance(value, str) and value.strip():
                    fields.append((f"{attr}{i}", value))
    return fields


def _harvest_patchouli_pairs(
    members: Mapping[str, bytes],
    source_lang: str,
    target_lang: str,
    keep: _PairFilter,
) -> list[dict[str, str]]:
    """Pairs from parallel patchouli_books/<book>/<locale>/ page files."""
    pairs: list[dict[str, str]] = []
    for member in sorted(members):
        match = _PATCHOULI_RE.search(member)
        if match is None or match.group(2) != source_lang:
            continue
        book, rel = match.group(1), match.group(3)
        counterpart = member[: match.start(2)] + target_lang + member[match.end(2) :]
        if counterpart not in members:
            continue
        source_fields = dict(_page_texts(_decode(members[member], member)))
        target_fields = dict(_page_texts(_decode(members[counterpart], counterpart)))
        for field in source_fields.keys() & target_fields.keys():
            if keep.keep(source_fields[field], target_fields[field]):
                pairs.append(
                    {
                        "key": f"patchouli:{book}/{rel}#{field}",
                        "category": "patchouli",
                        "source": source_fields[field].strip(),
                        "target": target_fields[field].strip(),
                    }
                )
    pairs.sort(key=lambda p: p["key"])
    return pairs


def harvest_pack(
    root: Path | str,
    *,
    source_lang: str = "en_us",
    target_lang: str = "ko_kr",
) -> dict[str, object]:
    """Harvest one modpack (zip or directory) into a goldset payload.

    The payload is fully deterministic for identical inputs (sorted
    members, sorted pair keys), so the file — and therefore the
    key-level split derived from it — is reproducible.
    """
    root = Path(root)
    members = dict(_iter_json_members(root))
    keep = _PairFilter(target_lang)
    pairs = _harvest_lang_pairs(members, source_lang, target_lang, keep)
    pairs += _harvest_patchouli_pairs(members, source_lang, target_lang, keep)

    by_category = Counter(p["category"] for p in pairs)
    logger.info(
        "Harvested %d gold pairs from %s (%s); dropped: %s",
        len(pairs),
        root.name,
        ", ".join(f"{c}={n}" for c, n in sorted(by_category.items())),
        dict(sorted(keep.dropped.items())) or "none",
    )
    return {
        "format": MODTEXT_FORMAT,
        "pack": _pack_name(root, members),
        "source_lang": source_lang,
        "target_lang": target_lang,
        "stats": {
            "pairs": len(pairs),
            "by_category": dict(sorted(by_category.items())),
            "dropped": dict(sorted(keep.dropped.items())),
        },
        "pairs": pairs,
    }


def load_goldset(path: Path | str) -> dict[str, object]:
    """Load and validate a harvested goldset payload."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != MODTEXT_FORMAT:
        raise ValueError(
            f"{path}: expected format '{MODTEXT_FORMAT}', got {data.get('format')!r}"
        )
    for field in ("pack", "source_lang", "target_lang", "pairs"):
        if field not in data:
            raise ValueError(f"{path}: missing goldset field '{field}'")
    return data
