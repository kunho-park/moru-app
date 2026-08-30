"""Shared/community TM rows are a fallback, never an override.

A shared row is keyed by the constant ``SHARED_GLOSSARY_VERSION``, so it is
visible on every run whatever the user's glossary is, and it never
invalidates. Two rules keep that from overriding user intent: a row stored
under the run's own glossary fingerprint wins, and a row is served only to
lang keys its ``key_scope`` covers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from moru_engine.tm import SHARED_GLOSSARY_VERSION, LocalTM, tm_key

#: The homograph the glossary's own key_scope docs use: "Wither" is the boss
#: under ``entity.*`` but the status effect under ``effect.*``.
BOSS_KEY = "entity.witherstormmod.wither_storm"
EFFECT_KEY = "effect.witherstormmod.wither_storm"
SOURCE = "Wither Storm"


@pytest.fixture
def tm(tmp_path: Path):
    with LocalTM(db_path=tmp_path / "tm.sqlite3") as tm:
        yield tm


def _store_shared(
    tm: LocalTM, source: str, target: str, key_scope: tuple[str, ...] = ()
) -> None:
    tm.store_many(
        [(source, target)],
        target_lang="ko_kr",
        glossary_version=SHARED_GLOSSARY_VERSION,
        origin="community",
        key_scope=key_scope,
    )


# --- precedence -----------------------------------------------------------


def test_local_row_wins_over_a_shared_row(tm: LocalTM) -> None:
    _store_shared(tm, SOURCE, "시듦 폭풍")
    tm.store(SOURCE, "ko_kr", "fp1", "위더 스톰")

    assert tm.lookup_many({BOSS_KEY: SOURCE}, "ko_kr", "fp1") == {BOSS_KEY: "위더 스톰"}


def test_shared_row_still_serves_when_there_is_no_local_row(tm: LocalTM) -> None:
    # The cold-start coverage the snapshot exists for is unchanged.
    _store_shared(tm, SOURCE, "시듦 폭풍")

    assert tm.lookup_many({BOSS_KEY: SOURCE}, "ko_kr", "fp1") == {
        BOSS_KEY: "시듦 폭풍"
    }


def test_a_glossary_edit_can_override_a_shared_row(tm: LocalTM) -> None:
    # The reported failure mode: editing the glossary changes the run
    # fingerprint, which invalidates the LOCAL row only — the shared row's
    # constant version never invalidates. Under the old precedence the
    # shared row then won forever and the glossary could not be applied.
    _store_shared(tm, SOURCE, "시듦 폭풍")
    tm.store(SOURCE, "ko_kr", "fp-old", "위더 스톰")

    # New fingerprint after the edit: the local row is gone, so the entry
    # must reach the model (a miss) rather than being pinned by the shared
    # row...
    assert tm.lookup_many({BOSS_KEY: SOURCE}, "ko_kr", "fp-new") == {
        BOSS_KEY: "시듦 폭풍"
    }
    # ...and once the run under the new glossary has translated it, THAT is
    # what every later run gets.
    tm.store(SOURCE, "ko_kr", "fp-new", "위더 스톰 (수정)")
    assert tm.lookup_many({BOSS_KEY: SOURCE}, "ko_kr", "fp-new") == {
        BOSS_KEY: "위더 스톰 (수정)"
    }


def test_a_run_whose_version_is_the_sentinel_probes_once(tm: LocalTM) -> None:
    _store_shared(tm, SOURCE, "시듦 폭풍")

    assert tm.lookup_many(
        {BOSS_KEY: SOURCE}, "ko_kr", SHARED_GLOSSARY_VERSION
    ) == {BOSS_KEY: "시듦 폭풍"}


# --- key scope ------------------------------------------------------------


def test_a_scoped_shared_row_serves_only_its_key_space(tm: LocalTM) -> None:
    _store_shared(tm, SOURCE, "시듦 폭풍", key_scope=("effect.*",))

    hits = tm.lookup_many(
        {EFFECT_KEY: SOURCE, BOSS_KEY: SOURCE}, "ko_kr", "fp1"
    )
    # The reading approved for the effect namespace cannot leak onto the
    # entity namespace, where it is the wrong sense.
    assert hits == {EFFECT_KEY: "시듦 폭풍"}


@pytest.mark.parametrize(
    ("scope", "key", "served"),
    [
        ((), "anything.at.all", True),  # unscoped covers every key
        (("entity.*",), "entity.mod.thing", True),
        (("entity.*",), "effect.mod.thing", False),
        (("entity.minecraft.wither",), "entity.minecraft.wither", True),
        (("entity.minecraft.wither",), "entity.minecraft.skeleton", False),
        (("subtitles.*.wither",), "subtitles.mod.wither", True),
        (("subtitles.*.wither",), "subtitles.mod.blaze", False),
        (("effect.*", "entity.*"), "entity.mod.thing", True),
    ],
)
def test_scope_globs_follow_the_glossary_semantics(
    tm: LocalTM, scope: tuple[str, ...], key: str, served: bool
) -> None:
    _store_shared(tm, SOURCE, "시듦 폭풍", key_scope=scope)

    hits = tm.lookup_many({key: SOURCE}, "ko_kr", "fp1")
    assert (key in hits) is served


def test_a_local_row_is_unscoped_and_serves_every_key(tm: LocalTM) -> None:
    tm.store(SOURCE, "ko_kr", "fp1", "위더 스톰")

    hits = tm.lookup_many({EFFECT_KEY: SOURCE, BOSS_KEY: SOURCE}, "ko_kr", "fp1")
    assert hits == {EFFECT_KEY: "위더 스톰", BOSS_KEY: "위더 스톰"}


def test_a_scoped_row_beats_nothing_when_the_key_is_absent(tm: LocalTM) -> None:
    # lookup() has no lang key to judge a scope against, so only unscoped
    # rows can match through it; that is what the keyword default means.
    _store_shared(tm, SOURCE, "시듦 폭풍", key_scope=("effect.*",))

    assert tm.lookup(SOURCE, "ko_kr", "fp1") is None
    assert tm.lookup(SOURCE, "ko_kr", "fp1", key=EFFECT_KEY) == "시듦 폭풍"


def test_scope_survives_a_reopen(tmp_path: Path) -> None:
    db = tmp_path / "tm.sqlite3"
    with LocalTM(db_path=db) as tm:
        _store_shared(tm, SOURCE, "시듦 폭풍", key_scope=("effect.*",))

    with LocalTM(db_path=db) as tm:
        assert tm.lookup(SOURCE, "ko_kr", "fp1", key=BOSS_KEY) is None
        assert tm.lookup(SOURCE, "ko_kr", "fp1", key=EFFECT_KEY) == "시듦 폭풍"


def test_resyncing_a_row_replaces_its_scope(tmp_path: Path, tm: LocalTM) -> None:
    _store_shared(tm, SOURCE, "시듦 폭풍", key_scope=("effect.*",))
    _store_shared(tm, SOURCE, "시듦 폭풍", key_scope=("entity.*",))

    assert tm.lookup(SOURCE, "ko_kr", "fp1", key=EFFECT_KEY) is None
    assert tm.lookup(SOURCE, "ko_kr", "fp1", key=BOSS_KEY) == "시듦 폭풍"


# --- backwards compatibility ---------------------------------------------

#: The exact schema shipped before rows carried a scope.
_PRE_CHANGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tm_entries (
    key_hash TEXT PRIMARY KEY,
    source_text TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    glossary_version TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'local',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tm_entries_target_lang ON tm_entries(target_lang);
CREATE TABLE IF NOT EXISTS tm_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _build_pre_change_db(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(_PRE_CHANGE_SCHEMA)
    for source, target, version, origin in (
        ("Iron Ingot", "철 주괴", "fp1", "local"),
        (SOURCE, "시듦 폭풍", SHARED_GLOSSARY_VERSION, "community"),
    ):
        conn.execute(
            "INSERT INTO tm_entries (key_hash, source_text, target_lang,"
            " glossary_version, translated_text, origin, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, '2026-01-01', '2026-01-01')",
            (
                tm_key(source, "ko_kr", version),
                source,
                "ko_kr",
                version,
                target,
                origin,
            ),
        )
    conn.execute(
        "INSERT INTO tm_meta (key, value) VALUES ('last_shared_version', 'v1')"
    )
    conn.commit()
    conn.close()


def test_a_pre_change_database_opens_and_keeps_working(tmp_path: Path) -> None:
    db = tmp_path / "tm.sqlite3"
    _build_pre_change_db(db)

    with LocalTM(db_path=db) as tm:
        # Existing rows still hit, and pre-change rows are unscoped, which
        # is exactly the behaviour they had.
        assert tm.lookup("Iron Ingot", "ko_kr", "fp1", key="item.iron") == "철 주괴"
        assert tm.lookup(SOURCE, "ko_kr", "fp1", key=BOSS_KEY) == "시듦 폭풍"
        # Bookkeeping survives, and new writes work against the migrated table.
        assert tm.get_meta("last_shared_version") == "v1"
        assert tm.stats().total_entries == 2
        tm.store("Gold Ingot", "ko_kr", "fp1", "금 주괴")
        assert tm.lookup("Gold Ingot", "ko_kr", "fp1") == "금 주괴"

    columns = _columns(db)
    assert "key_scope" in columns

    # Reopening is idempotent: the column is added once, rows are untouched.
    with LocalTM(db_path=db) as tm:
        assert tm.stats().total_entries == 3
        assert tm.lookup("Iron Ingot", "ko_kr", "fp1") == "철 주괴"
    assert _columns(db).count("key_scope") == 1


def _columns(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    names = [row[1] for row in conn.execute("PRAGMA table_info(tm_entries)")]
    conn.close()
    return names
