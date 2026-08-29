"""Patchouli keeps translating prose after the lang-key predicate moved.

``_is_translation_key_reference`` used to live on this handler. It is now
the shared ``handlers.base.is_translation_key_reference``, widened so a
numeric segment ("susy.quest.ql.1.desc") counts — a real BetterQuesting
value the old pattern let through as prose. Widening a predicate that
already gates a shipped handler changes what Patchouli translates, so both
directions are pinned here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moru_engine.handlers.patchouli import PatchouliHandler

#: The book shipped in the repo's sample modpack, verbatim.
SAMPLE_BOOK = Path("test/modpack/patchouli_books/testbook/en_us/entries/basics/intro.json")


def _write(path: Path, data: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _book_path(tmp_path: Path) -> Path:
    return tmp_path / "patchouli_books" / "testbook" / "en_us" / "entries" / "i.json"


async def test_real_sample_book_still_extracts_its_prose(tmp_path: Path) -> None:
    source = json.loads(SAMPLE_BOOK.read_text(encoding="utf-8"))
    path = _write(_book_path(tmp_path), source)

    entries = await PatchouliHandler().extract(path)

    assert entries == {
        "name": "Getting Started",
        "pages[0].text": (
            "Welcome to the $(l)Test Book$(). Use %s to navigate and press "
            "$(k:inventory) to open your inventory."
        ),
        "pages[1].title": "Next Steps",
        "pages[1].text": (
            "Craft an $(item)Enchanting Table$() when you gather enough resources."
        ),
    }


@pytest.mark.parametrize(
    "text",
    [
        "patchouli.confluence.otherworld_note.world.name",  # caught before too
        "mod.book.entry.1.name",  # NEWLY caught: numeric segment
        "twilightforest.book.page.2.title",
    ],
)
async def test_key_referencing_pages_are_skipped(tmp_path: Path, text: str) -> None:
    path = _write(_book_path(tmp_path), {"name": text, "pages": [{"text": text}]})

    assert await PatchouliHandler().extract(path) == {}


@pytest.mark.parametrize(
    "text",
    [
        "Craft an $(item)Enchanting Table$() when you gather resources.",
        "Version 1.20.1 of the pack changes this recipe.",
        "1.20.1",  # a page that is only a version string is still prose
        "Getting Started",
    ],
)
async def test_prose_pages_are_still_translated(tmp_path: Path, text: str) -> None:
    path = _write(_book_path(tmp_path), {"pages": [{"text": text}]})

    assert await PatchouliHandler().extract(path) == {"pages[0].text": text}
