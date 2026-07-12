"""Output generator: routing, fresh-only packs, full overrides, skips."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moru_engine.output import (
    FileOutput,
    OutputConfig,
    OutputGenerator,
    Route,
    route_for,
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _generator(tmp_path: Path) -> OutputGenerator:
    return OutputGenerator(
        OutputConfig(
            modpack_root=tmp_path / "modpack",
            output_dir=tmp_path / "out",
        )
    )


# -- routing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("mp/kubejs/assets/test/lang/en_us.json", Route.OVERRIDE),
        ("mp/config/ftbquests/quests/chapters/intro.snbt", Route.OVERRIDE),
        ("mp/patchouli_books/book/en_us/entries/a.json", Route.OVERRIDE),
        (
            "mp/.mct_cache/extracted/m.jar/assets/m/lang/en_us.json",
            Route.RESOURCE_PACK,
        ),
        ("mp/resourcepacks/pack/assets/m/lang/en_us.json", Route.RESOURCE_PACK),
        # jar-internal data/ would need .jar patching -> skipped
        (
            "mp/.mct_cache/extracted/m.jar/data/m/patchouli_books/b/en_us/x.json",
            Route.SKIP_JAR_DATA,
        ),
        # jar-internal config-like file: nothing on disk to overwrite
        (
            "mp/.mct_cache/extracted/m.jar/config/something/en_us.json",
            Route.SKIP_EXTRACTED,
        ),
        # a mod's builtin patchouli book rides along in the resource pack
        (
            "mp/.mct_cache/extracted/m.jar/assets/m/patchouli_books/b/en_us/x.json",
            Route.RESOURCE_PACK,
        ),
    ],
)
def test_route_for(path: str, expected: Route) -> None:
    assert route_for(Path(path)) == expected


# -- resource pack -------------------------------------------------------------


@pytest.mark.asyncio
async def test_resourcepack_lang_is_fresh_only_with_mcmeta(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "modpack/.mct_cache/extracted/m.jar/assets/m/lang/en_us.json",
        json.dumps({"a": "Alpha", "b": "Beta", "c": "Gamma"}),
    )
    gen = _generator(tmp_path)
    result = await gen.generate(
        [
            FileOutput(
                source_path=source,
                # "b" already had a translation in the modpack -> not fresh
                fresh={"a": "알파", "c": "감마"},
                full={"a": "알파", "b": "베타", "c": "감마"},
                namespace="m",
            )
        ]
    )

    out = tmp_path / "out/resourcepack/assets/m/lang/ko_kr.json"
    assert result.resourcepack_files == [out]
    data = json.loads(out.read_text(encoding="utf-8"))
    # pre-existing pairs are omitted: the game merges lang keys across
    # packs, and re-shipping them would shadow the modpack's own strings
    assert data == {"a": "알파", "c": "감마"}

    assert result.pack_mcmeta is not None
    mcmeta = json.loads(result.pack_mcmeta.read_text(encoding="utf-8"))
    assert mcmeta["pack"]["pack_format"] > 0
    assert "moru.gg" in mcmeta["pack"]["description"]

    # branded icon rides along and is part of the zipped file set
    assert result.pack_icon is not None
    assert result.pack_icon.name == "pack.png"
    assert result.pack_icon.stat().st_size > 0
    assert result.pack_icon in result.all_files


@pytest.mark.asyncio
async def test_same_namespace_sources_merge_into_one_lang_file(
    tmp_path: Path,
) -> None:
    jar = _write(
        tmp_path / "modpack/.mct_cache/extracted/m.jar/assets/m/lang/en_us.json",
        json.dumps({"a": "Alpha"}),
    )
    overlay = _write(
        tmp_path / "modpack/resourcepacks/pack/assets/m/lang/en_us.json",
        json.dumps({"b": "Beta"}),
    )
    gen = _generator(tmp_path)
    result = await gen.generate(
        [
            FileOutput(jar, fresh={"a": "알파"}, full={"a": "알파"}, namespace="m"),
            FileOutput(overlay, fresh={"b": "베타"}, full={"b": "베타"}, namespace="m"),
        ]
    )
    assert len(result.resourcepack_files) == 1
    data = json.loads(result.resourcepack_files[0].read_text(encoding="utf-8"))
    assert data == {"a": "알파", "b": "베타"}


@pytest.mark.asyncio
async def test_legacy_lang_file_keeps_lang_format(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "modpack/.mct_cache/extracted/m.jar/assets/m/lang/en_US.lang",
        "a=Alpha\n",
    )
    gen = _generator(tmp_path)
    result = await gen.generate(
        [FileOutput(source, fresh={"a": "알파"}, full={"a": "알파"}, namespace="m")]
    )
    out = tmp_path / "out/resourcepack/assets/m/lang/ko_KR.lang"
    assert result.resourcepack_files == [out]
    assert "a=알파" in out.read_text(encoding="utf-8")


# -- skips ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fully_pretranslated_file_is_skipped(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "modpack/.mct_cache/extracted/m.jar/assets/m/lang/en_us.json",
        json.dumps({"a": "Alpha"}),
    )
    gen = _generator(tmp_path)
    result = await gen.generate(
        [FileOutput(source, fresh={}, full={"a": "알파"}, namespace="m")]
    )
    assert result.skipped_existing == 1
    assert result.resourcepack_files == []
    # nothing shipped -> no pack.mcmeta either
    assert result.pack_mcmeta is None


@pytest.mark.asyncio
async def test_jar_data_files_are_skipped(tmp_path: Path) -> None:
    source = _write(
        tmp_path
        / "modpack/.mct_cache/extracted/m.jar/data/m/quests/chapter.json",
        json.dumps({"q": "Quest"}),
    )
    gen = _generator(tmp_path)
    result = await gen.generate(
        [FileOutput(source, fresh={"q": "퀘스트"}, full={"q": "퀘스트"})]
    )
    assert result.skipped_jar_data == 1
    assert result.all_files == []


# -- overrides -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_carries_full_merged_state(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "modpack/kubejs/assets/test/lang/en_us.json",
        json.dumps({"a": "Alpha", "b": "Beta"}),
    )
    gen = _generator(tmp_path)
    result = await gen.generate(
        [
            FileOutput(
                source_path=source,
                fresh={"a": "알파"},
                # "b" pre-existed in the modpack's ko_kr; the override file
                # replaces the whole file so it must keep it
                full={"a": "알파", "b": "베타"},
            )
        ]
    )
    out = tmp_path / "out/overrides/kubejs/assets/test/lang/ko_kr.json"
    assert result.override_files == [out]
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == {"a": "알파", "b": "베타"}
    # overrides alone never produce a pack.mcmeta
    assert result.pack_mcmeta is None


@pytest.mark.asyncio
async def test_stale_output_trees_are_wiped(tmp_path: Path) -> None:
    stale = _write(tmp_path / "out/resourcepack/assets/old/lang/ko_kr.json", "{}")
    _write(tmp_path / "out/overrides/config/old.snbt", "{}")
    gen = _generator(tmp_path)
    result = await gen.generate([])
    assert not stale.exists()
    assert not (tmp_path / "out/overrides/config/old.snbt").exists()
    assert result.all_files == []
