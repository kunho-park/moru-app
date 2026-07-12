"""moru.gg community snapshot sync (web-api.yaml manifest contract).

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

A manifest 404 (nothing published yet) is a clean no-op, never an error.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp
from platformdirs import user_config_dir

from .tm import META_LAST_SHARED_VERSION, LocalTM

logger = logging.getLogger(__name__)

__all__ = [
    "SHARED_TM_VERSION",
    "default_glossary_store_dir",
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
                # bundle; everything else is community-curated.
                synced = [
                    {
                        "source": str(e["source"]),
                        "target": str(e["target"]),
                        "origin": "vanilla"
                        if str(e.get("scope")) == "vanilla"
                        else "community",
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
                tm.store_many(
                    (
                        (str(e["source"]), str(e["target"]))
                        for e in entries
                        if e.get("source") and e.get("target")
                    ),
                    target_lang,
                    SHARED_TM_VERSION,
                    origin="community",
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
