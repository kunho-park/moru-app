"""Community client (community.py): snapshot sync + translation discovery."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from moru_engine.community import (
    download_translation,
    find_translation,
    load_user_glossary_terms,
    merge_extracted_terms,
    sync_community,
)
from moru_engine.scanner.pack_identity import PackIdentity, VersionRange
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
        {
            "source": "Void Orb",
            "target": "공허 구슬",
            "origin": "community",
            "key_scope": [],
        },
        {"source": "Creeper", "target": "크리퍼", "origin": "vanilla", "key_scope": []},
    ]


@pytest.mark.asyncio
async def test_local_row_outranks_shared_and_manual_rows_survive(
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

    # The local row was produced under exactly the glossary fingerprint the
    # run is using; the shared row is approved against no glossary and its
    # constant version never invalidates. So the local row wins — otherwise
    # editing the glossary could never override a community row, because
    # the edit invalidates only the local side. Community corrections still
    # reach the run through the shared GLOSSARY snapshot merged above, which
    # does change the fingerprint.
    assert tm.lookup("Storm Hammer", "ko_kr", "fp1") == "기계 번역"
    # The shared row remains the fallback wherever there is no local row,
    # which is what the snapshot exists for.
    assert tm.lookup("Storm Hammer", "ko_kr", "other-fp") == "폭풍 망치 (승인)"
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


# -- translation-pack discovery ------------------------------------------------


class FakeIndex:
    """Minimal moru.gg /api/translations/compatible index."""

    def __init__(self, *candidates: dict[str, Any]) -> None:
        self.candidates = list(candidates)
        self.queries: list[dict[str, str]] = []
        self.status = 200

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/translations/compatible", self._list)
        return app

    async def _list(self, request: web.Request) -> web.Response:
        self.queries.append(dict(request.query))
        if self.status != 200:
            return web.json_response({"error": "none"}, status=self.status)
        return web.json_response({"candidates": self.candidates})


def _candidate(version: str, **extra: Any) -> dict[str, Any]:
    """A published pack for the sample modpack (MC 1.20.1)."""
    return {
        "pack_id": f"pack-{version}",
        "modpack_version": version,
        "mc_version": "1.20.1",
        "target_lang": "ko_kr",
        "compatible_versions": None,
        "url": f"https://moru.gg/packs/pack-{version}",
        **extra,
    }


def _identity(version: str | None = "4.1.2", **extra: Any) -> PackIdentity:
    return PackIdentity(
        name="Society Sunlit Valley",
        version=version,
        mc_version="1.20.1",
        loader="forge",
        source="curseforge_manifest",
        confident=True,
        **extra,
    )


async def _index(aiohttp_server: Any, fake: FakeIndex) -> str:
    server = await aiohttp_server(fake.app())
    return f"http://{server.host}:{server.port}"


async def test_rangeless_pack_still_resolves_by_exact_version(
    aiohttp_server: Any,
) -> None:
    """Back-compat: a pack published before ranges existed matches its own
    modpack version and nothing else."""
    fake = FakeIndex(_candidate("4.1.2"), _candidate("4.0.0"))
    url = await _index(aiohttp_server, fake)

    match = await find_translation(url, _identity("4.1.2"), "ko_kr")

    assert match is not None
    assert (match.pack_id, match.exact) == ("pack-4.1.2", True)
    assert match.compatible_versions is None
    assert match.note == "4.1.2 버전용으로 제작된 번역팩입니다."

    # A minor bump finds nothing: no pack claimed anything about 4.1.3.
    assert await find_translation(url, _identity("4.1.3"), "ko_kr") is None


async def test_declared_range_covers_a_minor_modpack_bump(
    aiohttp_server: Any,
) -> None:
    fake = FakeIndex(
        _candidate("4.1.2", compatible_versions={"min": "4.1.2", "max": "4.2.0"})
    )
    url = await _index(aiohttp_server, fake)

    match = await find_translation(url, _identity("4.1.5"), "ko_kr")

    assert match is not None
    assert (match.pack_id, match.exact) == ("pack-4.1.2", False)
    assert match.compatible_versions == VersionRange(min="4.1.2", max="4.2.0")
    assert match.note == (
        "4.1.2 버전용 번역팩이지만 4.1.2~4.2.0 버전과 호환된다고 표시되어 있습니다."
    )

    # Outside the declared range -> nothing, however "close" it looks.
    assert await find_translation(url, _identity("4.3.0"), "ko_kr") is None


async def test_minecraft_version_is_a_hard_boundary(aiohttp_server: Any) -> None:
    """pack_format and the vanilla lang-key set change between releases, so a
    range never carries a pack across one."""
    fake = FakeIndex(
        _candidate(
            "4.1.2",
            mc_version="1.21.1",
            compatible_versions={"min": "4.1.2", "max": "9.9.9"},
        )
    )
    url = await _index(aiohttp_server, fake)

    assert await find_translation(url, _identity("4.1.5"), "ko_kr") is None
    # Not even the exact modpack version crosses it.
    assert await find_translation(url, _identity("4.1.2"), "ko_kr") is None


async def test_range_needs_the_boundary_known_on_both_sides(
    aiohttp_server: Any,
) -> None:
    """An unknown Minecraft version is the folder-name fallback: nothing was
    established, so only the exact version may match."""
    fake = FakeIndex(
        _candidate("4.1.2", compatible_versions={"min": "4.1.2", "max": "4.2.0"}),
        _candidate("3.0.0", mc_version=None),
    )
    url = await _index(aiohttp_server, fake)

    local = PackIdentity(name="Society Sunlit Valley", version="4.1.5")
    assert await find_translation(url, local, "ko_kr") is None

    # The candidate that carries no Minecraft version is capped the same way.
    exact = await find_translation(url, _identity("3.0.0"), "ko_kr")
    assert exact is not None
    assert (exact.pack_id, exact.exact) == ("pack-3.0.0", True)


async def test_unparseable_version_degrades_to_exact_match(
    aiohttp_server: Any,
) -> None:
    fake = FakeIndex(
        _candidate(
            "6.5.4hotfix",
            compatible_versions={"min": "6.5.4hotfix", "max": "6.6.0"},
        )
    )
    url = await _index(aiohttp_server, fake)

    # No ordering exists for "hotfix", so another version is never served.
    assert await find_translation(url, _identity("6.5.5"), "ko_kr") is None
    assert await find_translation(url, _identity("1.20.1-3.2b"), "ko_kr") is None

    match = await find_translation(url, _identity("v6.5.4hotfix"), "ko_kr")
    assert match is not None
    assert (match.pack_id, match.exact) == ("pack-6.5.4hotfix", True)


async def test_partial_match_reports_the_entries_it_does_not_cover(
    aiohttp_server: Any,
) -> None:
    """The primary report: per category, so a shrunken category cannot mask a
    grown one (the "they added a few optimization mods" case)."""
    fake = FakeIndex(
        _candidate(
            "4.1.2",
            compatible_versions={"min": "4.1.2", "max": "4.2.0"},
            total_entries=41_588,
            categories={"mods": 40_120, "quests": 1_468},
        )
    )
    url = await _index(aiohttp_server, fake)
    local = {"mods": 40_532, "quests": 1_300}

    match = await find_translation(
        url, _identity("4.2.0"), "ko_kr", local_categories=local
    )

    assert match is not None
    # quests shrank by 168, mods grew by 412: only the growth is claimed.
    assert match.uncovered_entries == 412
    assert match.uncovered_by_category == {"mods": 412}
    assert "최소 412개" in match.note

    # Without the local side there is no coverage claim at all.
    unmeasured = await find_translation(url, _identity("4.2.0"), "ko_kr")
    assert unmeasured is not None
    assert unmeasured.uncovered_entries is None
    assert "412" not in unmeasured.note


async def test_unaligned_category_names_compare_totals_not_partially(
    aiohttp_server: Any,
) -> None:
    """A registered pack's stats.categories uses the pipeline's coarse
    buckets ("scripts", "quests"), while the scan payload names categories
    for display ("KubeJS", "FTB Quests"). With no key in common the two sides
    collapse to totals - reporting each unmatched local category as fully
    uncovered would invent thousands of missing entries."""
    fake = FakeIndex(
        _candidate(
            "4.1.2",
            total_entries=8_036,
            categories={"quests": 1_657, "scripts": 5_688, "guidebook": 691},
        )
    )
    url = await _index(aiohttp_server, fake)
    scan_named = {"FTB Quests": 1_657, "KubeJS": 5_728, "Patchouli Books": 691}

    match = await find_translation(
        url, _identity("4.1.2"), "ko_kr", local_categories=scan_named
    )

    assert match is not None
    assert match.uncovered_entries == 40  # 8076 local - 8036 covered
    assert match.uncovered_by_category == {"": 40}


async def test_coverage_falls_back_to_the_total_for_a_pack_without_categories(
    aiohttp_server: Any,
) -> None:
    fake = FakeIndex(_candidate("4.1.2", total_entries=41_588))
    url = await _index(aiohttp_server, fake)

    match = await find_translation(
        url,
        _identity("4.1.2"),
        "ko_kr",
        local_categories={"mods": 40_532, "quests": 1_300},
    )

    assert match is not None
    assert match.uncovered_entries == 244  # 41832 local - 41588 covered
    assert match.uncovered_by_category == {"": 244}


async def test_fully_covered_match_says_so_without_a_percentage(
    aiohttp_server: Any,
) -> None:
    fake = FakeIndex(
        _candidate("4.1.2", total_entries=41_588, categories={"mods": 41_588})
    )
    url = await _index(aiohttp_server, fake)

    match = await find_translation(
        url, _identity("4.1.2"), "ko_kr", local_categories={"mods": 41_000}
    )

    assert match is not None
    assert (match.uncovered_entries, match.uncovered_by_category) == (0, {})
    assert "빠진 항목은 확인되지 않았습니다" in match.note
    assert "%" not in match.note


async def test_best_match_prefers_exact_then_coverage_then_newest(
    aiohttp_server: Any,
) -> None:
    ranged = {"min": "4.0.0", "max": "4.9.9"}
    fake = FakeIndex(
        _candidate("4.0.0", compatible_versions=ranged, categories={"mods": 42_000}),
        _candidate("4.1.0", compatible_versions=ranged, categories={"mods": 100}),
        _candidate("4.1.5", compatible_versions=ranged, categories={"mods": 90}),
    )
    url = await _index(aiohttp_server, fake)
    local = {"mods": 41_000}

    # Coverage outranks recency: the newest pack covers almost nothing.
    covered = await find_translation(
        url, _identity("4.2.0"), "ko_kr", local_categories=local
    )
    assert covered is not None
    assert (covered.pack_id, covered.uncovered_entries) == ("pack-4.0.0", 0)

    # Unmeasured, the newest compatible version wins instead.
    newest = await find_translation(url, _identity("4.2.0"), "ko_kr")
    assert newest is not None and newest.pack_id == "pack-4.1.5"

    # An exact match outranks both.
    exact = await find_translation(
        url, _identity("4.1.0"), "ko_kr", local_categories=local
    )
    assert exact is not None
    assert (exact.pack_id, exact.exact) == ("pack-4.1.0", True)


async def test_nothing_published_is_a_clean_none(aiohttp_server: Any) -> None:
    fake = FakeIndex()
    url = await _index(aiohttp_server, fake)
    assert await find_translation(url, _identity(), "ko_kr") is None

    fake.status = 404
    assert await find_translation(url, _identity(), "ko_kr") is None


async def test_query_narrows_by_curseforge_id_and_skips_a_nameless_pack(
    aiohttp_server: Any,
) -> None:
    fake = FakeIndex(_candidate("4.1.2"))
    url = await _index(aiohttp_server, fake)

    await find_translation(
        url, _identity("4.1.2", curseforge_project_id=925200), "ko_kr"
    )
    assert fake.queries[-1] == {
        "target_lang": "ko_kr",
        "modpack_name": "Society Sunlit Valley",
        "curseforge_id": "925200",
        "modpack_version": "4.1.2",
        "mc_version": "1.20.1",
    }

    # Nothing to narrow by -> no request at all.
    assert await find_translation(url, PackIdentity(version="4.1.2"), "ko_kr") is None
    assert len(fake.queries) == 1


# -- translation-pack download -------------------------------------------------


class FakePackHost:
    """Minimal /api/packs/{id}/download: one body per published kind."""

    def __init__(self, **bodies: bytes) -> None:
        self.bodies = bodies
        self.kinds: list[str | None] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/packs/{pack_id}/download", self._download)
        return app

    async def _download(self, request: web.Request) -> web.StreamResponse:
        kind = request.query.get("kind")
        self.kinds.append(kind)
        body = self.bodies.get(kind or "resource_pack")
        if body is None:
            return web.json_response({"error": "no such file"}, status=404)
        return web.Response(body=body, content_type="application/zip")


async def _pack_host(aiohttp_server: Any, fake: FakePackHost) -> str:
    server = await aiohttp_server(fake.app())
    return f"http://{server.host}:{server.port}/api/packs/p1/download"


async def test_download_fetches_both_channels_as_migration_ready_zips(
    aiohttp_server: Any, tmp_path: Path
) -> None:
    """The two archives map onto the two migration inputs.

    Saved as ZIPs on purpose: the migration index already accepts a folder
    or a ZIP, so extracting here would only duplicate a tested path.
    """
    fake = FakePackHost(resource_pack=b"RP-ZIP-BYTES", overrides=b"OV-ZIP-BYTES")
    url = await _pack_host(aiohttp_server, fake)

    archives = await download_translation(url, tmp_path / "p1")

    assert set(archives) == {"resource_pack", "overrides"}
    assert archives["resource_pack"].read_bytes() == b"RP-ZIP-BYTES"
    assert archives["overrides"].read_bytes() == b"OV-ZIP-BYTES"
    assert fake.kinds == ["resource_pack", "overrides"]


async def test_a_channel_the_pack_never_published_is_absent_not_an_error(
    aiohttp_server: Any, tmp_path: Path
) -> None:
    """Most packs ship a resource pack and no overrides archive.

    The download route answers 404 for a kind the pack has no file for, which
    is a statement about the pack rather than a failure.
    """
    fake = FakePackHost(resource_pack=b"RP")
    url = await _pack_host(aiohttp_server, fake)

    archives = await download_translation(url, tmp_path / "p1")

    assert set(archives) == {"resource_pack"}
    assert not (tmp_path / "p1" / "overrides.zip").exists()


async def test_a_pack_with_nothing_downloadable_yields_nothing(
    aiohttp_server: Any, tmp_path: Path
) -> None:
    fake = FakePackHost()
    url = await _pack_host(aiohttp_server, fake)

    assert await download_translation(url, tmp_path / "p1") == {}


async def test_an_oversized_body_leaves_no_half_written_archive(
    aiohttp_server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncated archive must never survive as a usable pack.

    The part file is only renamed into place once the body completes, so an
    aborted transfer cannot be picked up by a later run as a real pack and
    silently reused as half a translation.
    """
    monkeypatch.setattr("moru_engine.community._MAX_PACK_BYTES", 8)
    fake = FakePackHost(resource_pack=b"x" * 4096)
    url = await _pack_host(aiohttp_server, fake)

    with pytest.raises(ValueError, match="size cap"):
        await download_translation(url, tmp_path / "p1")

    assert list((tmp_path / "p1").iterdir()) == []


async def test_kind_is_appended_to_a_url_that_already_has_a_query(
    aiohttp_server: Any, tmp_path: Path
) -> None:
    """Downloads select a variant with ?variant=; the kind must join it."""
    fake = FakePackHost(resource_pack=b"RP")
    base = await _pack_host(aiohttp_server, fake)

    archives = await download_translation(f"{base}?variant=plain", tmp_path / "p1")

    assert set(archives) == {"resource_pack"}
    assert fake.kinds == ["resource_pack", "overrides"]
