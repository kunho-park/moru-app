"""Tests for the local SQLite translation memory."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from moru_engine.tm import META_LAST_SHARED_VERSION, LocalTM, tm_key


@pytest.fixture
def tm(tmp_path: Path):
    with LocalTM(db_path=tmp_path / "tm.sqlite3") as tm:
        yield tm


def test_miss_store_hit_roundtrip(tm: LocalTM) -> None:
    assert tm.lookup("Enchanting Table", "ko_kr", "g1") is None

    tm.store("Enchanting Table", "ko_kr", "g1", "마법 부여대")

    assert tm.lookup("Enchanting Table", "ko_kr", "g1") == "마법 부여대"
    # Other dimensions of the key still miss.
    assert tm.lookup("Enchanting Table", "ja_jp", "g1") is None
    assert tm.lookup("Enchanting table", "ko_kr", "g1") is None


def test_glossary_version_change_invalidates_hit(tm: LocalTM) -> None:
    tm.store("Vault Altar", "ko_kr", "g1", "볼트 제단")

    assert tm.lookup("Vault Altar", "ko_kr", "g1") == "볼트 제단"
    assert tm.lookup("Vault Altar", "ko_kr", "g2") is None


def test_store_many_lookup_many_batch(tm: LocalTM) -> None:
    tm.store_many(
        [
            ("Iron Ingot", "철 주괴"),
            ("Gold Ingot", "금 주괴"),
        ],
        target_lang="ko_kr",
        glossary_version="g1",
    )

    entries = {
        "item.iron": "Iron Ingot",
        "item.gold": "Gold Ingot",
        "item.iron_dup": "Iron Ingot",  # duplicate source shares the hit
        "item.unknown": "Netherite Ingot",  # miss: absent from result
    }
    hits = tm.lookup_many(entries, target_lang="ko_kr", glossary_version="g1")

    assert hits == {
        "item.iron": "철 주괴",
        "item.gold": "금 주괴",
        "item.iron_dup": "철 주괴",
    }
    # Wrong glossary version misses everything.
    assert tm.lookup_many(entries, "ko_kr", "g2") == {}
    assert tm.lookup_many({}, "ko_kr", "g1") == {}


def test_upsert_updates_translation(tm: LocalTM) -> None:
    tm.store("Creeper", "ko_kr", "g1", "크리퍼 (Creeper)")
    tm.store("Creeper", "ko_kr", "g1", "크리퍼", origin="shared")

    assert tm.lookup("Creeper", "ko_kr", "g1") == "크리퍼"

    stats = tm.stats()
    assert stats.total_entries == 1
    assert stats.by_origin == {"shared": 1}


def test_upsert_preserves_created_at_and_bumps_updated_at(tm: LocalTM) -> None:
    tm.store("Blaze Rod", "ko_kr", "g1", "블레이즈 막대기")
    key = tm_key("Blaze Rod", "ko_kr", "g1")

    conn = sqlite3.connect(tm.db_path)
    created_before, updated_before = conn.execute(
        "SELECT created_at, updated_at FROM tm_entries WHERE key_hash = ?", (key,)
    ).fetchone()

    tm.store("Blaze Rod", "ko_kr", "g1", "블레이즈 막대")

    created_after, updated_after = conn.execute(
        "SELECT created_at, updated_at FROM tm_entries WHERE key_hash = ?", (key,)
    ).fetchone()
    conn.close()

    assert created_after == created_before
    assert updated_after >= updated_before


def test_stats_counts_and_shared_version(tm: LocalTM) -> None:
    assert tm.stats().total_entries == 0
    assert tm.stats().last_shared_version is None

    tm.store("A", "ko_kr", "g1", "가")
    tm.store("B", "ko_kr", "g1", "나", origin="shared")
    tm.store("C", "ja_jp", "g1", "ハ", origin="shared")
    tm.set_meta(META_LAST_SHARED_VERSION, "2026-07-01")

    stats = tm.stats()
    assert stats.total_entries == 3
    assert stats.by_origin == {"local": 1, "shared": 2}
    assert stats.last_shared_version == "2026-07-01"


def test_tm_key_components_do_not_bleed() -> None:
    # The unit separator keeps field boundaries unambiguous.
    assert tm_key("ab", "c", "g") != tm_key("a", "bc", "g")
    assert tm_key("x", "ko_kr", "g1") == tm_key("x", "ko_kr", "g1")


def test_persistence_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "tm.sqlite3"
    with LocalTM(db_path=db) as tm:
        tm.store("Oak Planks", "ko_kr", "g1", "참나무 판자")

    with LocalTM(db_path=db) as tm:
        assert tm.lookup("Oak Planks", "ko_kr", "g1") == "참나무 판자"


def test_creates_parent_dirs(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "deeper" / "tm.sqlite3"
    with LocalTM(db_path=db) as tm:
        tm.store("Torch", "ko_kr", "g1", "횃불")
        assert db.exists()
        assert tm.lookup("Torch", "ko_kr", "g1") == "횃불"
