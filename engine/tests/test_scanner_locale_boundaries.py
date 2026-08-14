"""Locale detection must respect boundaries, not raw substrings.

CTE2's quest chapters "act_ii.snbt" and "act_iv.snbt" both contain a
locale-shaped chunk mid-filename ("ct_ii" / "ct_iv" before the dot). The
old pairing key stripped those as locales, collapsing both files onto the
same dict key — whichever the filesystem globbed last silently swallowed
the other, so exactly one chapter vanished from every scan.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moru_engine.scanner import scan_modpack
from moru_engine.scanner.modpack_scanner import ModpackScanner


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


_CHAPTER = """{{
\tfilename: "{name}"
\tid: "{cid}"
\torder_index: 0
\tquests: [{{
\t\tid: "{cid}F"
\t\ttitle: "Quest of {name}"
\t\tx: 0.0d
\t\ty: 0.0d
\t}}]
}}
"""


@pytest.mark.asyncio
async def test_roman_numeral_chapters_all_survive_pairing(tmp_path: Path) -> None:
    modpack = tmp_path / "modpack"
    chapters = modpack / "config" / "ftbquests" / "quests" / "chapters"
    names = ("act_0", "act_i", "act_ii", "act_iii", "act_iv", "act_v")
    for i, name in enumerate(names):
        _write(
            chapters / f"{name}.snbt",
            _CHAPTER.format(name=name, cid=f"{i:016X}"),
        )

    result = await scan_modpack(modpack)
    found = sorted(p.source_path.name for p in result.all_translation_pairs)

    assert found == sorted(f"{name}.snbt" for name in names)


def test_base_path_keeps_roman_numeral_files_distinct() -> None:
    scanner = ModpackScanner()
    ii = scanner._get_base_path("config/ftbquests/quests/chapters/act_ii.snbt")
    iv = scanner._get_base_path("config/ftbquests/quests/chapters/act_iv.snbt")
    assert ii != iv

    # Real locale forms still collapse to one pairing key.
    assert scanner._get_base_path(
        "assets/mod/lang/en_us.json"
    ) == scanner._get_base_path("assets/mod/lang/ko_kr.json")
    assert scanner._get_base_path(
        "patchouli_books/almanac/en_us/entries/apple.json"
    ) == scanner._get_base_path("patchouli_books/almanac/ko_kr/entries/apple.json")
    # Suffix-style locale filenames pair as well.
    assert scanner._get_base_path("config/foo_en_us.json") == scanner._get_base_path(
        "config/foo_ko_kr.json"
    )


def test_locale_classification_ignores_mid_word_chunks() -> None:
    scanner = ModpackScanner()  # en_us -> ko_kr
    # "tako_kraken" contains the target locale as a raw substring; treating
    # it as a target file would drop it from translation entirely.
    assert not scanner._path_has_locale("config/quests/tako_kraken.json", "ko_kr")
    assert not scanner._path_has_locale("config/chapters/act_ii.snbt", "ct_ii")
    assert scanner._path_has_locale("assets/mod/lang/ko_kr.json", "ko_kr")
    assert scanner._path_has_locale("books/guide/ko_kr/page.json", "ko_kr")
    assert scanner._path_has_locale("config/foo_ko_kr.json", "ko_kr")


@pytest.mark.asyncio
async def test_lang_pairing_still_works_end_to_end(tmp_path: Path) -> None:
    """The boundary fix must not break real source/target pairing."""
    modpack = tmp_path / "modpack"
    lang = modpack / "kubejs" / "assets" / "testmod" / "lang"
    _write(lang / "en_us.json", json.dumps({"item.testmod.gem": "Gem"}))
    _write(lang / "ko_kr.json", json.dumps({"item.testmod.gem": "보석"}))

    result = await scan_modpack(modpack)

    assert result.total_paired == 1
    (pair,) = result.paired_files
    assert pair.source_path.name == "en_us.json"
    assert pair.target_path is not None and pair.target_path.name == "ko_kr.json"
