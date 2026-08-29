"""Human translations outrank machine cache in the translation memory.

Without this a translator's terminology decisions die with the session: the
next automatic run re-asks the model about strings a human already settled, and
worse, may answer differently. Precedence is `manual` > `local` > community.

The degenerate-pair gate is deliberately NOT bypassed for human rows. A target
equal to its source is kept in that entry's own output — the validator rates
`UNTRANSLATED` a warning precisely so a proper noun still ships — but it must
not become a global cache promise, because a TM row is keyed on source text
alone and would suppress translation of that string everywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from moru_engine.tm import (
    MANUAL_ORIGIN,
    SHARED_GLOSSARY_VERSION,
    LocalTM,
    is_cacheable_pair,
)

SRC = "Osmium Ingot"
FINGERPRINT = "abc123def456"


@pytest.fixture
def tm(tmp_path: Path):
    store = LocalTM(tmp_path / "tm.sqlite3")
    yield store
    store.close()


def _lookup(tm: LocalTM, key: str = "item.testmod.ingot") -> str | None:
    return tm.lookup_many({key: SRC}, "ko_kr", FINGERPRINT).get(key)


# ---- precedence ---------------------------------------------------------


def test_manual_outranks_a_machine_row(tm: LocalTM):
    tm.store(SRC, "ko_kr", FINGERPRINT, "기계 번역", "local")
    tm.store(SRC, "ko_kr", SHARED_GLOSSARY_VERSION, "사람 번역", MANUAL_ORIGIN)
    assert _lookup(tm) == "사람 번역"


def test_manual_outranks_a_community_row(tm: LocalTM):
    tm.store(SRC, "ko_kr", SHARED_GLOSSARY_VERSION, "커뮤니티", "community")
    tm.store(SRC, "ko_kr", SHARED_GLOSSARY_VERSION, "사람 번역", MANUAL_ORIGIN)
    assert _lookup(tm) == "사람 번역"


def test_machine_still_outranks_community(tm: LocalTM):
    """The pre-existing precedence must not be disturbed by adding a rank."""
    tm.store(SRC, "ko_kr", SHARED_GLOSSARY_VERSION, "커뮤니티", "community")
    tm.store(SRC, "ko_kr", FINGERPRINT, "기계 번역", "local")
    assert _lookup(tm) == "기계 번역"


def test_precedence_is_independent_of_write_order(tm: LocalTM):
    """Ranked by origin, not by which probe or write happened last."""
    tm.store(SRC, "ko_kr", SHARED_GLOSSARY_VERSION, "사람 번역", MANUAL_ORIGIN)
    tm.store(SRC, "ko_kr", FINGERPRINT, "기계 번역", "local")
    assert _lookup(tm) == "사람 번역"


def test_a_human_row_survives_a_later_machine_write(tm: LocalTM):
    """A later automatic run must not bury the human's decision."""
    tm.store(SRC, "ko_kr", SHARED_GLOSSARY_VERSION, "사람 번역", MANUAL_ORIGIN)
    for i in range(3):
        tm.store(SRC, "ko_kr", FINGERPRINT, f"기계 {i}", "local")
    assert _lookup(tm) == "사람 번역"


def test_an_unknown_future_origin_does_not_outrank_a_human(tm: LocalTM):
    tm.store(SRC, "ko_kr", SHARED_GLOSSARY_VERSION, "사람 번역", MANUAL_ORIGIN)
    tm.store(SRC, "ko_kr", FINGERPRINT, "미래 출처", "something_new")
    assert _lookup(tm) == "사람 번역"


def test_a_human_row_is_served_across_glossary_fingerprints(tm: LocalTM):
    """A human's choice does not depend on the glossary that was active.

    Stored under the shared sentinel for exactly this reason: a local row keyed
    to one fingerprint stops being served the moment a glossary term changes,
    which is right for a machine result and wrong for a human decision.
    """
    tm.store(SRC, "ko_kr", SHARED_GLOSSARY_VERSION, "사람 번역", MANUAL_ORIGIN)
    for fingerprint in ("abc123def456", "totally-different", "another-one"):
        hit = tm.lookup_many({"k": SRC}, "ko_kr", fingerprint).get("k")
        assert hit == "사람 번역"


# ---- the gate stays ----------------------------------------------------


def test_a_human_target_equal_to_its_source_is_not_cached(tm: LocalTM):
    """The narrow rule: no exemption from the degenerate-pair gate.

    A translator deliberately keeping a proper noun in English is correct for
    that entry, but caching "X -> X" is a permanent global promise that would
    stop the model ever being asked about that string again.
    """
    assert is_cacheable_pair("Mekanism", "Mekanism") is False
    tm.store("Mekanism", "ko_kr", SHARED_GLOSSARY_VERSION, "Mekanism", MANUAL_ORIGIN)
    assert tm.lookup_many({"k": "Mekanism"}, "ko_kr", FINGERPRINT) == {}
    assert tm.stats().total_entries == 0


def test_a_human_blank_target_is_not_cached(tm: LocalTM):
    tm.store(SRC, "ko_kr", SHARED_GLOSSARY_VERSION, "   ", MANUAL_ORIGIN)
    assert _lookup(tm) is None


def test_stats_counts_human_rows_under_their_own_origin(tm: LocalTM):
    """`by_origin` is what surfaces a "hand translated N" figure."""
    tm.store(SRC, "ko_kr", SHARED_GLOSSARY_VERSION, "사람 번역", MANUAL_ORIGIN)
    tm.store("Steel Gear", "ko_kr", FINGERPRINT, "강철 기어", "local")
    stats = tm.stats()
    assert stats.total_entries == 2
    assert stats.by_origin[MANUAL_ORIGIN] == 1
    assert stats.by_origin["local"] == 1
