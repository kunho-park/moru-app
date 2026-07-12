"""Community snapshot sync (community.py): manifest, merge, no-op paths."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from moru_engine.community import (
    load_user_glossary_terms,
    merge_extracted_terms,
    sync_community,
)
from moru_engine.tm import META_LAST_SHARED_VERSION, LocalTM


def _snapshot_gz(kind: str, lang: str, version: str, entries: list[dict]) -> bytes:
    body = {
        "kind": kind,
        "lang": lang,
        "version": version,
        "entry_count": len(entries),
        "entries": entries,
    }
    return gzip.compress(json.dumps(body).encode("utf-8"))


class FakeWeb:
    """Minimal moru.gg: manifest endpoints + R2-style snapshot bodies."""

    def __init__(self) -> None:
        self.snapshots: dict[str, tuple[str, list[dict]]] = {}  # kind -> (ver, entries)
        self.manifest_hits = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/{kind}/manifest", self._manifest)
        app.router.add_get("/r2/{kind}.json.gz", self._body)
        return app

    async def _manifest(self, request: web.Request) -> web.Response:
        self.manifest_hits += 1
        kind = request.match_info["kind"]
        snap = self.snapshots.get(kind)
        if snap is None:
            return web.json_response({"error": "none"}, status=404)
        version, entries = snap
        return web.json_response(
            {
                "version": version,
                "hash": "x",
                "size": 1,
                "url": f"http://{request.host}/r2/{kind}.json.gz",
                "entry_count": len(entries),
            }
        )

    async def _body(self, request: web.Request) -> web.Response:
        kind = request.match_info["kind"]
        version, entries = self.snapshots[kind]
        lang = "ko_kr"
        return web.Response(
            body=_snapshot_gz(kind, lang, version, entries),
            content_type="application/gzip",
        )


@pytest.fixture
def tm(tmp_path: Path) -> LocalTM:
    with LocalTM(tmp_path / "tm.sqlite3") as db:
        yield db


async def _serve(aiohttp_server: Any, fake: FakeWeb) -> str:
    server = await aiohttp_server(fake.app())
    return f"http://{server.host}:{server.port}"


@pytest.mark.asyncio
async def test_sync_merges_tm_and_glossary(
    aiohttp_server: Any, tm: LocalTM, tmp_path: Path
) -> None:
    fake = FakeWeb()
    fake.snapshots["tm"] = (
        "20260711000000",
        [{"entry_key": "k", "source": "Storm Hammer", "target": "폭풍 망치", "pack_id": "p"}],
    )
    fake.snapshots["glossary"] = (
        "20260711000001",
        [
            {"source": "Void Orb", "target": "공허 구슬", "scope": "global", "notes": None},
            {"source": "Creeper", "target": "크리퍼", "scope": "vanilla", "notes": None},
        ],
    )
    url = await _serve(aiohttp_server, fake)
    store = tmp_path / "glossaries"

    result = await sync_community(url, "en_us", "ko_kr", tm, store)

    assert result["tm"] == {"version": "20260711000000", "entries": 1, "updated": True}
    assert result["glossary"] == {
        "version": "20260711000001",
        "terms": 2,
        "updated": True,
    }
    # TM row hits under ANY per-run glossary fingerprint (shared sentinel)...
    assert tm.lookup("Storm Hammer", "ko_kr", "whatever-fingerprint") == "폭풍 망치"
    assert tm.get_meta(META_LAST_SHARED_VERSION) == "20260711000000"
    # ...and the glossary store carries the snapshot rows with scope-mapped
    # origins: scope=vanilla -> origin=vanilla, everything else community.
    terms = load_user_glossary_terms(store, "en_us", "ko_kr")
    assert terms == [
        {"source": "Void Orb", "target": "공허 구슬", "origin": "community"},
        {"source": "Creeper", "target": "크리퍼", "origin": "vanilla"},
    ]


@pytest.mark.asyncio
async def test_shared_row_outranks_local_and_manual_rows_survive(
    aiohttp_server: Any, tm: LocalTM, tmp_path: Path
) -> None:
    # Machine-cached local row under the run fingerprint...
    tm.store("Storm Hammer", "ko_kr", "fp1", "기계 번역", origin="local")
    # ...plus a store carrying every origin: locally-owned rows (manual/
    # extracted) must survive, server-owned rows (vanilla/community) must
    # be replaced wholesale by the new snapshot.
    store = tmp_path / "glossaries"
    store.mkdir()
    (store / "en_us_ko_kr.json").write_text(
        json.dumps(
            {
                "source_lang": "en_us",
                "target_lang": "ko_kr",
                "terms": [
                    {"source": "Ember Gem", "target": "잉걸 보석", "origin": "manual"},
                    {"source": "Mined", "target": "채굴 용어", "origin": "extracted"},
                    {"source": "Old", "target": "옛", "origin": "community"},
                    {"source": "Stale", "target": "옛 바닐라", "origin": "vanilla"},
                ],
            }
        ),
        encoding="utf-8",
    )
    fake = FakeWeb()
    fake.snapshots["tm"] = (
        "v2",
        [{"source": "Storm Hammer", "target": "폭풍 망치 (승인)", "pack_id": "p"}],
    )
    fake.snapshots["glossary"] = (
        "v2",
        [
            {"source": "Void Orb", "target": "공허 구슬", "scope": "global", "notes": ""},
            {"source": "Creeper", "target": "크리퍼", "scope": "vanilla", "notes": ""},
        ],
    )
    url = await _serve(aiohttp_server, fake)

    await sync_community(url, "en_us", "ko_kr", tm, store)

    # Human-approved community row wins over the fingerprint-local row.
    assert tm.lookup("Storm Hammer", "ko_kr", "fp1") == "폭풍 망치 (승인)"
    terms = load_user_glossary_terms(store, "en_us", "ko_kr")
    origins = {t["source"]: t["origin"] for t in terms}
    # Manual/extracted rows kept; stale community AND vanilla rows replaced.
    assert origins == {
        "Ember Gem": "manual",
        "Mined": "extracted",
        "Void Orb": "community",
        "Creeper": "vanilla",
    }


@pytest.mark.asyncio
async def test_unchanged_version_is_noop_and_404_is_clean(
    aiohttp_server: Any, tm: LocalTM, tmp_path: Path
) -> None:
    fake = FakeWeb()
    url = await _serve(aiohttp_server, fake)
    store = tmp_path / "glossaries"

    # Nothing published yet -> both None, no error.
    result = await sync_community(url, "en_us", "ko_kr", tm, store)
    assert result == {"glossary": None, "tm": None}

    fake.snapshots["tm"] = ("v1", [{"source": "A", "target": "가", "pack_id": "p"}])
    first = await sync_community(url, "en_us", "ko_kr", tm, store)
    assert first["tm"]["updated"] is True

    second = await sync_community(url, "en_us", "ko_kr", tm, store)
    assert second["tm"]["updated"] is False  # same version -> manifest-only no-op
    assert tm.stats().by_origin.get("community") == 1


@pytest.mark.asyncio
async def test_multi_megabyte_snapshot_survives_chunked_transfer(
    aiohttp_server: Any, tm: LocalTM, tmp_path: Path
) -> None:
    # Regression: a single StreamReader.read(n) returns only the first
    # buffered chunk (~64KB), truncating multi-MB gzip bodies mid-stream
    # ("Compressed file ended before the end-of-stream marker").
    # High-entropy entries keep the COMPRESSED body far above one chunk.
    fake = FakeWeb()
    entries = [
        {
            "source": f"Entry {i} {hashlib.sha256(str(i).encode()).hexdigest()}",
            "target": f"번역 {i} {hashlib.sha256(str(-i).encode()).hexdigest()}",
        }
        for i in range(20_000)
    ]
    assert len(_snapshot_gz("tm", "ko_kr", "v-big", entries)) > 512 * 1024
    fake.snapshots["tm"] = ("v-big", entries)
    base = await _serve(aiohttp_server, fake)

    result = await sync_community(base, "en_us", "ko_kr", tm, tmp_path / "glossaries")

    assert result["tm"]["updated"] is True
    assert tm.stats().by_origin.get("community") == 20_000


def test_merge_extracted_terms_appends_without_shadowing(tmp_path: Path) -> None:
    store = tmp_path / "glossaries"

    added = merge_extracted_terms(
        store, "en_us", "ko_kr", [("Storm Hammer", "폭풍 망치"), ("Mana", "마나")]
    )
    assert added == 2

    # Existing rows win regardless of origin and case; blanks are dropped.
    added = merge_extracted_terms(
        store,
        "en_us",
        "ko_kr",
        [("storm hammer", "다른 번역"), ("  ", "무시"), ("Ingot", "주괴")],
    )
    assert added == 1

    terms = load_user_glossary_terms(store, "en_us", "ko_kr")
    assert {(t["source"], t["target"], t["origin"]) for t in terms} == {
        ("Storm Hammer", "폭풍 망치", "extracted"),
        ("Mana", "마나", "extracted"),
        ("Ingot", "주괴", "extracted"),
    }

    # Nothing new -> no rewrite, count 0.
    assert merge_extracted_terms(store, "en_us", "ko_kr", [("Mana", "마나")]) == 0
