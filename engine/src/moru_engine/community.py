"""moru.gg community client (web-api.yaml contract).

Pull-only client for the web platform's published TM / glossary snapshots:

- ``GET {web}/api/tm/manifest?lang=`` / ``/api/glossary/manifest?lang=``
  return a few-KB manifest ``{version, hash, size, url, entry_count}``.
- The gzip JSON body is downloaded straight from R2 (never via Vercel).

Merge targets:

- TM entries land in :class:`LocalTM` with ``origin="community"`` under the
  :data:`SHARED_TM_VERSION` sentinel so lookups hit regardless of the
  per-run glossary fingerprint (community corrections are human-approved
  and outrank machine-cached rows).
- Glossary terms land in the engine's user glossary store
  (``glossaries/{src}_{tgt}.json`` - the hub screen's document). Snapshot
  entries with ``scope == "vanilla"`` become ``origin="vanilla"`` rows
  (the web platform now publishes the vanilla bundle), everything else
  ``origin="community"``. Each sync replaces the previous vanilla and
  community rows and leaves manual/extracted rows untouched. The pipeline
  merges this store into every run's glossary.

Translation-pack discovery (:func:`find_translation`) uses the same base
URL: ``GET {web}/api/translations/compatible`` lists the packs published
for one modpack, and the *decision* of whether one fits the local pack is
made here — only this side knows the pack actually on disk.

A manifest 404 (nothing published yet) is a clean no-op, never an error.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from platformdirs import user_config_dir

from .scanner.pack_identity import (
    PackIdentity,
    VersionRange,
    normalize_mc_version,
    pack_version_key,
    parse_version_range,
    same_pack_version,
)
from .tm import META_LAST_SHARED_VERSION, LocalTM

logger = logging.getLogger(__name__)

__all__ = [
    "SHARED_TM_VERSION",
    "TranslationMatch",
    "default_glossary_store_dir",
    "find_translation",
    "load_user_glossary_terms",
    "merge_extracted_terms",
    "sync_community",
]

#: ``glossary_version`` sentinel for shared community TM rows. Community
#: corrections are approved against no particular local glossary, so they
#: are keyed by this constant and consulted on every lookup.
SHARED_TM_VERSION = "shared"

#: ``tm_meta`` key prefix for the last merged glossary snapshot version.
_GLOSSARY_VERSION_META = "community_glossary_version:{lang}"

_TIMEOUT = aiohttp.ClientTimeout(total=15)
#: Snapshot bodies are a few MB at most; hard cap against a bad URL.
_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
#: Candidate cap for one discovery call. A modpack has a handful of
#: published translations per language, never pages of them.
_MAX_CANDIDATES = 50


def default_glossary_store_dir() -> Path:
    """The engine's user glossary store directory (matches server/app.py)."""
    return Path(user_config_dir("moru", "moru")) / "glossaries"


def glossary_store_path(store_dir: Path, source_lang: str, target_lang: str) -> Path:
    return store_dir / f"{source_lang}_{target_lang}.json"


def load_user_glossary_terms(
    store_dir: Path, source_lang: str, target_lang: str
) -> list[dict[str, Any]]:
    """Read the hub glossary document's terms; [] when absent/corrupt."""
    path = glossary_store_path(store_dir, source_lang, target_lang)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    terms = doc.get("terms") if isinstance(doc, dict) else None
    return terms if isinstance(terms, list) else []


def _write_glossary_store(
    store_dir: Path,
    source_lang: str,
    target_lang: str,
    terms: list[dict[str, Any]],
) -> None:
    path = glossary_store_path(store_dir, source_lang, target_lang)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"source_lang": source_lang, "target_lang": target_lang, "terms": terms}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def merge_extracted_terms(
    store_dir: Path,
    source_lang: str,
    target_lang: str,
    pairs: list[tuple[str, str]],
) -> int:
    """Append pipeline-extracted (source, target) pairs to the store.

    Existing rows win regardless of origin - vanilla/community/manual
    translations are never shadowed by extraction. Returns rows added.
    """
    if not pairs:
        return 0
    terms = load_user_glossary_terms(store_dir, source_lang, target_lang)
    seen = {str(t.get("source") or "").strip().lower() for t in terms}
    added = 0
    for source, target in pairs:
        source, target = source.strip(), target.strip()
        key = source.lower()
        if not source or not target or key in seen:
            continue
        terms.append({"source": source, "target": target, "origin": "extracted"})
        seen.add(key)
        added += 1
    if added:
        _write_glossary_store(store_dir, source_lang, target_lang, terms)
    return added


async def _fetch_manifest(
    session: aiohttp.ClientSession, base: str, kind: str, lang: str
) -> dict[str, Any] | None:
    """Manifest dict, or None when no snapshot is published (404)."""
    async with session.get(
        f"{base}/api/{kind}/manifest", params={"lang": lang}
    ) as resp:
        if resp.status == 404:
            return None
        resp.raise_for_status()
        return await resp.json()


async def _fetch_snapshot(
    session: aiohttp.ClientSession, url: str
) -> list[dict[str, Any]]:
    """Download and decode a snapshot body -> its entries list."""
    # NB: StreamReader.read(n) returns as soon as ANY buffered data is
    # available (up to n bytes), so a single read truncates multi-MB
    # bodies mid-gzip. Accumulate chunks until EOF, capped for safety.
    chunks: list[bytes] = []
    total = 0
    async with session.get(url) as resp:
        resp.raise_for_status()
        async for chunk in resp.content.iter_chunked(1 << 20):
            total += len(chunk)
            if total > _MAX_SNAPSHOT_BYTES:
                raise ValueError("snapshot body exceeds size cap")
            chunks.append(chunk)
    raw = b"".join(chunks)
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    body = json.loads(raw)
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise ValueError("snapshot body has no entries list")
    return entries


async def sync_community(
    web_url: str,
    source_lang: str,
    target_lang: str,
    tm: LocalTM,
    glossary_store_dir: Path,
) -> dict[str, Any]:
    """One pull: merge fresh community TM + glossary snapshots.

    Returns ``{"glossary": {version, terms, updated} | None,
    "tm": {version, entries, updated} | None}`` (None = nothing published).
    Unchanged versions are cheap no-ops (manifest fetch only).
    """
    base = web_url.rstrip("/")
    result: dict[str, Any] = {"glossary": None, "tm": None}

    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        manifest = await _fetch_manifest(session, base, "glossary", target_lang)
        if manifest is not None:
            meta_key = _GLOSSARY_VERSION_META.format(lang=target_lang)
            version = str(manifest["version"])
            updated = False
            if tm.get_meta(meta_key) != version:
                entries = await _fetch_snapshot(session, str(manifest["url"]))
                # scope=="vanilla" entries are the web-published vanilla
                # bundle; everything else is community-curated. Note the
                # snapshot's "scope" is that origin marker - a term's lang-key
                # scope is the separate "key_scope" list (see TermRule).
                synced = [
                    {
                        "source": str(e["source"]),
                        "target": str(e["target"]),
                        "origin": "vanilla"
                        if str(e.get("scope")) == "vanilla"
                        else "community",
                        "key_scope": [str(s) for s in (e.get("key_scope") or [])],
                    }
                    for e in entries
                    if e.get("source") and e.get("target")
                ]
                # Server-owned origins are replaced wholesale each sync;
                # locally-owned rows (manual/extracted) survive.
                kept = [
                    t
                    for t in load_user_glossary_terms(
                        glossary_store_dir, source_lang, target_lang
                    )
                    if t.get("origin") not in ("community", "vanilla")
                ]
                _write_glossary_store(
                    glossary_store_dir, source_lang, target_lang, kept + synced
                )
                tm.set_meta(meta_key, version)
                updated = True
                logger.info(
                    "Community glossary %s: %d terms merged", version, len(synced)
                )
            result["glossary"] = {
                "version": version,
                "terms": int(manifest.get("entry_count") or 0),
                "updated": updated,
            }

        manifest = await _fetch_manifest(session, base, "tm", target_lang)
        if manifest is not None:
            version = str(manifest["version"])
            updated = False
            if tm.get_meta(META_LAST_SHARED_VERSION) != version:
                entries = await _fetch_snapshot(session, str(manifest["url"]))
                # Shared rows are keyed by a constant version, so they are
                # visible on every run whatever the user's glossary is.
                # Their "key_scope" is what keeps a reading out of a key
                # space it was never approved for (the homograph case
                # TermRule.key_scope exists for), so it has to survive the
                # merge. store_many takes one scope per call; group by it.
                by_scope: dict[tuple[str, ...], list[tuple[str, str]]] = {}
                for e in entries:
                    if not (e.get("source") and e.get("target")):
                        continue
                    scope = tuple(
                        sorted(
                            {str(s).strip() for s in (e.get("key_scope") or [])}
                            - {""}
                        )
                    )
                    by_scope.setdefault(scope, []).append(
                        (str(e["source"]), str(e["target"]))
                    )
                for scope, pairs in by_scope.items():
                    tm.store_many(
                        pairs,
                        target_lang,
                        SHARED_TM_VERSION,
                        origin="community",
                        key_scope=scope,
                    )
                tm.set_meta(META_LAST_SHARED_VERSION, version)
                updated = True
                logger.info(
                    "Community TM %s: %d entries merged", version, len(entries)
                )
            result["tm"] = {
                "version": version,
                "entries": int(manifest.get("entry_count") or 0),
                "updated": updated,
            }

    return result


# -- translation-pack discovery ------------------------------------------------


@dataclass
class TranslationMatch:
    """A published community translation pack offered for the local modpack.

    The version match only decides *candidacy*. What a user can act on is
    ``uncovered_entries``: measured over 11,513 real published modpack
    versions, a declared range can carry a translation across only ~25% of
    real updates (most version strings are not orderable releases at all),
    so the coverage report — not the range — is what makes offering a
    non-exact match worthwhile.

    ``exact`` marks the modpack version the translation was actually built
    for, the only thing that matched before ranges existed.
    """

    pack_id: str
    modpack_version: str | None
    exact: bool
    compatible_versions: VersionRange | None
    total_entries: int | None
    #: Lower bound on local source entries this translation does not cover.
    #: None when the local side was not measured; 0 means no missing entry
    #: could be established, which is as far as counts go.
    uncovered_entries: int | None
    #: The same figure per scan category, fully-covered categories dropped.
    #: "mods +412" is the "they added a few optimization mods" case, told
    #: precisely enough to act on.
    uncovered_by_category: dict[str, int]
    url: str | None
    download_url: str | None
    note: str


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 else None


def _covered_counts(candidate: dict[str, Any], total: int | None) -> dict[str, int]:
    """Per-category entry counts a published pack covers.

    ``stats.categories`` rides along with every registration (web-api.yaml);
    a pack registered before that field has only its total, which becomes a
    single unnamed bucket.
    """
    categories = candidate.get("categories")
    if isinstance(categories, dict):
        counts = {
            str(name): count
            for name, value in categories.items()
            if (count := _positive_int(value)) is not None
        }
        if counts:
            return counts
    return {"": total} if total is not None else {}


def _uncovered(
    local: Mapping[str, int] | None, covered: dict[str, int]
) -> tuple[int | None, dict[str, int]]:
    """Local entries a translation demonstrably does not cover.

    Deliberately a lower bound: holding fewer entries in a bucket than the
    local pack has means at least the difference is untranslated, whatever
    the keys are. It cannot see an entry that was *replaced* rather than
    added, which is exactly why nothing here quotes a coverage percentage —
    that would claim precision only a key-level diff of the downloaded pack
    can establish.

    Per category when the two sides name their categories the same way, so a
    category that shrank can never mask one that grew. When they do not
    overlap at all — a pack registered with only a total, or a caller keying
    the local map differently from the published ``stats.categories`` — both
    sides collapse to one bucket and the same subtraction runs on the totals.
    Never a partial comparison: an unmatched key would be reported as an
    entirely uncovered category.
    """
    if local is None or not covered:
        return None, {}
    if set(local) & set(covered):
        buckets = dict(local)
    else:
        buckets = {"": sum(local.values())}
        covered = {"": sum(covered.values())}
    gaps = {
        name: count - covered.get(name, 0)
        for name, count in buckets.items()
        if count > covered.get(name, 0)
    }
    return sum(gaps.values()), gaps


def _match_note(
    version: str | None,
    exact: bool,
    declared: VersionRange | None,
    uncovered: int | None,
) -> str:
    """What was matched and what it costs, for the user to read."""
    shown = version or "알 수 없는"
    if exact:
        note = f"{shown} 버전용으로 제작된 번역팩입니다."
    else:
        span = f"{declared.min}~{declared.max}" if declared else shown
        note = (
            f"{shown} 버전용 번역팩이지만 {span} 버전과 호환된다고 표시되어 있습니다."
        )
    if uncovered:
        note += (
            f" 현재 모드팩에는 이 번역팩에 없는 항목이 최소 {uncovered}개 있으며,"
            " 해당 항목은 번역되지 않습니다."
        )
    elif uncovered == 0:
        note += " 카테고리별 항목 수를 비교한 결과 빠진 항목은 확인되지 않았습니다."
    return note


def _candidate_match(
    identity: PackIdentity,
    candidate: dict[str, Any],
    local_categories: Mapping[str, int] | None,
) -> TranslationMatch | None:
    """Judge one published pack against the local identity; None = no offer.

    Three rules, in order:

    1. Minecraft version is a hard boundary. ``pack_format`` and the vanilla
       lang-key set change across releases, so a pack built for another
       release is never offered — and a range is only ever honoured when the
       boundary could actually be checked on both sides (an unknown
       ``mc_version`` is the folder-name fallback, i.e. nothing was
       established about the local pack at all).
    2. The exact modpack version always matches, range or no range. This is
       what keeps packs published before the field resolving as before.
    3. Any other version needs the pack's declared range to cover it.
    """
    pack_id = str(candidate.get("pack_id") or "").strip()
    if not pack_id:
        return None

    local_mc = normalize_mc_version(identity.mc_version)
    candidate_mc = normalize_mc_version(candidate.get("mc_version"))
    if local_mc is not None and candidate_mc is not None and local_mc != candidate_mc:
        return None

    version = str(candidate.get("modpack_version") or "").strip() or None
    declared = parse_version_range(candidate.get("compatible_versions"))
    exact = same_pack_version(identity.version, version)
    if not exact:
        boundary_known = local_mc is not None and candidate_mc is not None
        if not boundary_known or declared is None:
            return None
        if not declared.contains(identity.version):
            return None

    total_entries = _positive_int(candidate.get("total_entries"))
    uncovered, gaps = _uncovered(
        local_categories, _covered_counts(candidate, total_entries)
    )
    return TranslationMatch(
        pack_id=pack_id,
        modpack_version=version,
        exact=exact,
        compatible_versions=declared,
        total_entries=total_entries,
        uncovered_entries=uncovered,
        uncovered_by_category=gaps,
        url=str(candidate.get("url") or "") or None,
        download_url=str(candidate.get("download_url") or "") or None,
        note=_match_note(version, exact, declared, uncovered),
    )


async def find_translation(
    web_url: str,
    identity: PackIdentity,
    target_lang: str,
    local_categories: Mapping[str, int] | None = None,
) -> TranslationMatch | None:
    """Best published translation pack for the local modpack, or None.

    ``GET {web}/api/translations/compatible`` narrows by CurseForge id when
    the identity carries one and by pack name otherwise; every compatibility
    rule is then applied here by :func:`_candidate_match`, because the
    platform knows neither the Minecraft version on disk nor how much
    content the local pack has.

    ``local_categories`` is a ``{category: untranslated_entry_count}`` map of
    the local pack. Pass it: without it a non-exact match can only be
    reported as "the author says this fits", and with it the user is told how
    many of their entries the translation demonstrably misses — the honest
    form of "97% covered", and the part of this feature that carries its
    weight. Key it the way the published ``stats.categories`` is keyed to get
    the per-category comparison; a map keyed any other way still yields the
    correct total-based figure (see :func:`_uncovered`).

    Preference order: the exact version, then the fewest uncovered entries,
    then the newest compatible version, then the most entries covered. A 404
    or an empty candidate list is a clean None, never an error.
    """
    if identity.curseforge_project_id is None and not identity.name:
        return None  # nothing to narrow by; the platform cannot answer

    params: dict[str, str] = {"target_lang": target_lang}
    if identity.name:
        params["modpack_name"] = identity.name
    if identity.curseforge_project_id is not None:
        params["curseforge_id"] = str(identity.curseforge_project_id)
    if identity.version:
        params["modpack_version"] = identity.version
    if identity.mc_version:
        params["mc_version"] = identity.mc_version

    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.get(
            f"{web_url.rstrip('/')}/api/translations/compatible", params=params
        ) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            body = await resp.json()

    candidates = body.get("candidates") if isinstance(body, dict) else None
    if not isinstance(candidates, list):
        return None

    matches: list[TranslationMatch] = []
    for candidate in candidates[:_MAX_CANDIDATES]:
        if not isinstance(candidate, dict):
            continue
        match = _candidate_match(identity, candidate, local_categories)
        if match is not None:
            matches.append(match)
    if not matches:
        return None

    best = max(
        matches,
        key=lambda m: (
            m.exact,
            -(m.uncovered_entries or 0),
            pack_version_key(m.modpack_version) or (),
            m.total_entries or 0,
        ),
    )
    logger.info(
        "Community translation %s matched (%s, exact=%s, uncovered=%s)",
        best.pack_id,
        best.modpack_version,
        best.exact,
        best.uncovered_entries,
    )
    return best
