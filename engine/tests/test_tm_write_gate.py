"""Degenerate pairs must never be cached, written OR served.

A cache entry skips the model on every later run under the same glossary
fingerprint, so an entry whose "translation" is just its source would pin
that string to its untranslated form forever. The validator rates that
condition a WARNING on purpose (a proper noun that correctly reads the
same must still reach the output), so the cache is what has to refuse it.

Guarding the read as well as the write is what makes rows written before
the gate existed harmless without a migration scan at startup.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from moru_engine.tm import LocalTM, is_cacheable_pair, tm_key


@pytest.fixture
def tm(tmp_path: Path):
    with LocalTM(db_path=tmp_path / "tm.sqlite3") as tm:
        yield tm


def _insert_unguarded(
    db: Path,
    source: str,
    translated: str,
    *,
    target_lang: str = "ko_kr",
    glossary_version: str = "g1",
) -> None:
    """Write a row the way a pre-gate build did, bypassing store()."""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT OR REPLACE INTO tm_entries (key_hash, source_text, target_lang,"
        " glossary_version, translated_text, origin, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'local', '2026-01-01', '2026-01-01')",
        (
            tm_key(source, target_lang, glossary_version),
            source,
            target_lang,
            glossary_version,
            translated,
        ),
    )
    conn.commit()
    conn.close()


# --- the predicate --------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "translated"),
    [
        ("Right-click to open", "Right-click to open"),  # the poisoning case
        ("Minecraft", "Minecraft"),  # proper noun that stays identical
        ("100", "100"),  # bare number
        ("+", "+"),  # bare symbol
        ("§6Iron Ingot", "Iron Ingot"),  # only a formatting code differs
        ("Iron Ingot", "iron ingot"),  # only case differs
        ("Iron Ingot", ""),  # empty target
        ("Iron Ingot", "   \n"),  # whitespace-only target
        ("", "철 주괴"),  # blank source: unreachable row
        ("   ", "철 주괴"),
        ("Level %s", "레벨 {{ARG}}"),  # restoration failed or was bypassed
        ("§6Gold§r", "{{COLOR}}금{{RESET}}"),
    ],
)
def test_degenerate_pairs_are_rejected(source: str, translated: str) -> None:
    assert is_cacheable_pair(source, translated) is False


@pytest.mark.parametrize(
    ("source", "translated"),
    [
        ("Iron Ingot", "철 주괴"),
        ("Right-click to open", "우클릭하여 열기"),
        ("Level %s", "레벨 %s"),  # placeholders survive a real translation
        ("§6Gold§r", "§6금§r"),
        ("Iron Ingot", "Eisenbarren"),
    ],
)
def test_real_translations_are_accepted(source: str, translated: str) -> None:
    assert is_cacheable_pair(source, translated) is True


# --- the write path -------------------------------------------------------


def test_untranslated_copy_never_becomes_a_cache_hit(tm: LocalTM) -> None:
    tm.store("Right-click to open", "ko_kr", "g1", "Right-click to open")

    assert tm.lookup("Right-click to open", "ko_kr", "g1") is None
    assert tm.stats().total_entries == 0


def test_identical_proper_noun_stays_out_without_blocking_its_neighbours(
    tm: LocalTM,
) -> None:
    # Rejection is per pair: the mod name is skipped, the real translation
    # beside it is stored. The entry itself is NOT failed anywhere — this
    # module only decides what may be cached.
    tm.store_many(
        [
            ("Minecraft", "Minecraft"),
            ("Enchanting Table", "마법 부여대"),
        ],
        target_lang="ko_kr",
        glossary_version="g1",
    )

    assert tm.lookup("Minecraft", "ko_kr", "g1") is None
    assert tm.lookup("Enchanting Table", "ko_kr", "g1") == "마법 부여대"
    assert tm.stats().total_entries == 1


def test_blank_and_token_bearing_targets_are_refused(tm: LocalTM) -> None:
    tm.store("Iron Ingot", "ko_kr", "g1", "")
    tm.store("Gold Ingot", "ko_kr", "g1", "  \t ")
    tm.store("Level %s", "ko_kr", "g1", "레벨 {{ARG}}")
    tm.store("", "ko_kr", "g1", "철 주괴")

    assert tm.stats().total_entries == 0


def test_store_many_refuses_only_the_degenerate_rows(tm: LocalTM) -> None:
    tm.store_many(
        [
            ("Iron Ingot", "철 주괴"),
            ("Creeper", "Creeper"),
            ("Gold Ingot", "금 주괴"),
            ("Torch", ""),
        ],
        target_lang="ko_kr",
        glossary_version="g1",
    )

    hits = tm.lookup_many(
        {
            "item.iron": "Iron Ingot",
            "entity.creeper": "Creeper",
            "item.gold": "Gold Ingot",
            "block.torch": "Torch",
        },
        target_lang="ko_kr",
        glossary_version="g1",
    )
    assert hits == {"item.iron": "철 주괴", "item.gold": "금 주괴"}


def test_store_many_collapses_a_repeated_source_to_one_row(tm: LocalTM) -> None:
    # A pack repeats the same string across mods, so the caller hands the
    # same source over many times. The upsert would leave the last value.
    tm.store_many(
        [("Health", "체력"), ("Speed", "속도"), ("Health", "생명력")],
        target_lang="ko_kr",
        glossary_version="g1",
    )

    assert tm.stats().total_entries == 2
    assert tm.lookup("Health", "ko_kr", "g1") == "생명력"


def test_a_refused_pair_does_not_overwrite_a_good_row(tm: LocalTM) -> None:
    tm.store("Enchanting Table", "ko_kr", "g1", "마법 부여대")
    tm.store("Enchanting Table", "ko_kr", "g1", "Enchanting Table")

    assert tm.lookup("Enchanting Table", "ko_kr", "g1") == "마법 부여대"


# --- the read path: rows written before the gate existed ------------------


def test_a_pre_gate_poisoned_row_is_never_served(tmp_path: Path) -> None:
    db = tmp_path / "tm.sqlite3"
    with LocalTM(db_path=db) as tm:
        tm.store("Enchanting Table", "ko_kr", "g1", "마법 부여대")
    # A pre-gate build could write these; nothing removes them at open.
    _insert_unguarded(db, "Right-click to open", "Right-click to open")
    _insert_unguarded(db, "Iron Ingot", "")

    with LocalTM(db_path=db) as tm:
        # Still on disk, and cheap to leave there...
        assert tm.stats().total_entries == 3
        # ...because they can never be served.
        assert tm.lookup("Right-click to open", "ko_kr", "g1") is None
        assert tm.lookup("Iron Ingot", "ko_kr", "g1") is None
        assert tm.lookup("Enchanting Table", "ko_kr", "g1") == "마법 부여대"


def test_opening_does_not_scan_or_delete(tmp_path: Path) -> None:
    # The scan is O(rows) with a Python callback per row, so it must not
    # happen in the constructor: correctness comes from the read gate.
    db = tmp_path / "tm.sqlite3"
    with LocalTM(db_path=db):
        pass
    _insert_unguarded(db, "Right-click to open", "Right-click to open")

    with LocalTM(db_path=db) as tm:
        assert tm.stats().total_entries == 1


def test_purge_degenerate_reclaims_only_the_dead_rows(tmp_path: Path) -> None:
    db = tmp_path / "tm.sqlite3"
    with LocalTM(db_path=db) as tm:
        tm.store("Enchanting Table", "ko_kr", "g1", "마법 부여대")
    _insert_unguarded(db, "Right-click to open", "Right-click to open")
    _insert_unguarded(db, "§6Iron Ingot", "iron ingot")

    with LocalTM(db_path=db) as tm:
        assert tm.purge_degenerate() == 2
        assert tm.purge_degenerate() == 0
        assert tm.stats().total_entries == 1
        assert tm.lookup("Enchanting Table", "ko_kr", "g1") == "마법 부여대"


def test_purge_reports_zero_on_a_clean_store(tm: LocalTM) -> None:
    tm.store("Iron Ingot", "ko_kr", "g1", "철 주괴")

    assert tm.purge_degenerate() == 0
    assert tm.stats().total_entries == 1
