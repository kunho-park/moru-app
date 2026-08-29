"""A JAR's own data/ tree is the one place a translation cannot be installed.

Patchouli's ``BookRegistry.init`` walks each mod container's files directly
and opens the hit with ``Files.newInputStream(mod.getPath(file))``, so
``book.json`` never passes through the resource-pack or data-pack stack —
nothing outside the JAR can shadow it. The loss is therefore real and must
be reported, but only when there is something to lose: most mods put a lang
KEY in those fields, and that key's text lives in the JAR's own lang file,
which does get translated and does ship.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moru_engine.output.generator import (
    FileOutput,
    GenerationResult,
    JarDataLoss,
    OutputConfig,
    OutputGenerator,
    Route,
    jar_name_for,
    route_for,
)

#: data/botania/patchouli_books/lexicon/book.json, verbatim from
#: Botania-1.20.1-450-FORGE.jar. name/landing_text are lang KEYS, and both
#: resolve in the same JAR's assets/botania/lang/en_us.json
#: ("Lexica Botania", "Magic, Tech. Naturally.$(br2)…").
BOTANIA_BOOK = {
    "name": "item.botania.lexicon",
    "landing_text": "botania.landing",
    "version": "buildNumber",
    "book_texture": "patchouli:textures/gui/book_green.png",
    "model": "botania:lexicon",
    "open_sound": "botania:lexicon_open",
    "creative_tab": "botania",
    "custom_book_item": "botania:lexicon",
}

#: data/mimi/patchouli_books/guide/book.json from
#: mimimod-1.20.1-4.3.1.BETA.2-forge.jar: the one jar of 330 that inlines
#: prose instead of a key.
MIMI_BOOK = {
    "name": "MIMI and Me",
    "landing_text": (
        "$(l)M$()usical    ---> $(l)M$()usical$(br)$(l)I$()nstrument  ---> "
        "$(l)I$()nstrument$(br2)This guide will teach you all there is to "
        "know about using MIMI to play music in Minecraft!"
    ),
    "custom_book_item": "mimi:guide",
    "use_resource_pack": "true",
}


def _write(path: Path, data: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _jar_book(root: Path, jar: str, namespace: str, book: str) -> Path:
    return (
        root
        / "modpack/.mct_cache/extracted"
        / jar
        / "data"
        / namespace
        / "patchouli_books"
        / book
        / "book.json"
    )


def _generator(root: Path) -> OutputGenerator:
    return OutputGenerator(
        OutputConfig(modpack_root=root / "modpack", output_dir=root / "out")
    )


# --- attribution ----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".mct_cache/extracted/Botania-1.20.1-450-FORGE.jar/data/b/x.json",
         "Botania-1.20.1-450-FORGE.jar"),
        # Case is preserved: the JAR name is shown to the user.
        (".mct_cache/Extracted/MiMi.jar/data/mimi/book.json", "MiMi.jar"),
        (r".mct_cache\extracted\m.jar\data\m\book.json", "m.jar"),
        # Not from an archive: nothing to attribute.
        ("config/betterquesting/DefaultQuests.json", ""),
        (".mct_cache/extracted", ""),
    ],
)
def test_jar_attribution_from_the_extraction_path(path: str, expected: str) -> None:
    assert jar_name_for(path) == expected


# --- routing is unchanged -------------------------------------------------


def test_jar_assets_still_ride_along_but_jar_data_cannot(tmp_path: Path) -> None:
    # The distinction the whole report rests on: a book's BODY under assets/
    # ships in the resource pack; only its data/ definition is stranded.
    assets = ".mct_cache/extracted/m.jar/assets/m/patchouli_books/g/en_us/e.json"
    data = ".mct_cache/extracted/m.jar/data/m/patchouli_books/g/book.json"

    assert route_for(assets) is Route.RESOURCE_PACK
    assert route_for(data) is Route.SKIP_JAR_DATA


# --- only a real loss is reported ----------------------------------------


async def test_a_lang_key_only_book_is_not_reported_as_a_loss(
    tmp_path: Path,
) -> None:
    # Botania's title IS translated — through the JAR's lang file, which
    # ships in the resource pack. Reporting this file would send the user
    # after a loss that does not exist.
    source = _write(
        _jar_book(tmp_path, "Botania-1.20.1-450-FORGE.jar", "botania", "lexicon"),
        BOTANIA_BOOK,
    )

    result = await _generator(tmp_path).generate(
        [FileOutput(source, fresh={}, full={})]
    )

    assert result.jar_data_losses == []
    assert result.skipped_jar_data == 0
    assert result.jar_data_loss_mods == []


async def test_a_prose_book_is_reported_with_its_mod_and_entry_count(
    tmp_path: Path,
) -> None:
    source = _write(
        _jar_book(tmp_path, "mimimod-1.20.1-4.3.1.BETA.2-forge.jar", "mimi", "guide"),
        MIMI_BOOK,
    )
    fresh = {
        "name": "[ko] MIMI and Me",
        "landing_text": "[ko] 이 가이드는…",
    }

    result = await _generator(tmp_path).generate(
        [FileOutput(source, fresh=fresh, full=fresh)]
    )

    assert result.jar_data_losses == [
        JarDataLoss(
            source_path=str(source),
            mod="mimimod-1.20.1-4.3.1.BETA.2-forge.jar",
            entry_count=2,
        )
    ]
    assert result.skipped_jar_data == 1
    assert result.jar_data_loss_mods == ["mimimod-1.20.1-4.3.1.BETA.2-forge.jar"]
    # Nothing was installable, so nothing was written.
    assert result.all_files == []


async def test_a_mixed_pack_reports_only_the_jars_that_lost_text(
    tmp_path: Path,
) -> None:
    # The measured shape of a real 330-jar pack: several lang-key-only books
    # and one that inlines prose.
    quiet = [
        FileOutput(
            _write(_jar_book(tmp_path, jar, ns, book), BOTANIA_BOOK),
            fresh={},
            full={},
        )
        for jar, ns, book in (
            ("Botania-1.20.1-450-FORGE.jar", "botania", "lexicon"),
            ("chococraft-1.20.1-forge-0.9.12.jar", "chococraft", "chocopedia"),
            ("lightmanscurrency-1.20.1-2.3.0.0a.jar", "lc", "trader_guide"),
            ("sebastrnlib-4.0.0.jar", "sebastrnlib", "guide"),
        )
    ]
    loud = FileOutput(
        _write(_jar_book(tmp_path, "mimimod.jar", "mimi", "guide"), MIMI_BOOK),
        fresh={"name": "[ko]"},
        full={"name": "[ko]"},
    )

    result = await _generator(tmp_path).generate([*quiet, loud])

    assert result.skipped_jar_data == 1
    assert result.jar_data_loss_mods == ["mimimod.jar"]


async def test_repeated_losses_from_one_jar_collapse_in_the_mod_list(
    tmp_path: Path,
) -> None:
    files = [
        FileOutput(
            _write(_jar_book(tmp_path, "m.jar", "m", book), MIMI_BOOK),
            fresh={"name": "[ko]"},
            full={"name": "[ko]"},
        )
        for book in ("guide", "second_guide")
    ]

    result = await _generator(tmp_path).generate(files)

    assert result.skipped_jar_data == 2
    assert result.jar_data_loss_mods == ["m.jar"]


def test_unattributable_losses_are_left_out_of_the_mod_list() -> None:
    # A path with no extraction segment still counts as a loss; it just has
    # no JAR to name, and an empty name must never reach the user as a mod.
    result = GenerationResult(
        resourcepack_dir=Path("rp"),
        overrides_dir=Path("ov"),
        jar_data_losses=[
            JarDataLoss(source_path="data/m/x.json", mod="", entry_count=1),
            JarDataLoss(source_path="e/m.jar/data/m/y.json", mod="m.jar", entry_count=3),
        ],
    )

    assert result.skipped_jar_data == 2
    assert result.jar_data_loss_mods == ["m.jar"]
