"""Deterministic glossary candidate mining (glossary/term_miner.py)."""

from __future__ import annotations

from moru_engine.glossary.term_miner import mine_candidates


def test_name_key_values_are_candidates_at_count_one() -> None:
    cands = mine_candidates(
        {"item.mymod.storm_hammer": "Storm hammer"},
        existing_terms=set(),
    )
    assert [c.term for c in cands] == ["Storm hammer"]
    assert cands[0].from_name_key is True


def test_capitalized_phrases_need_min_count() -> None:
    entries = {
        "quest.1.desc": "Bring the Void Orb to the altar.",
        "quest.2.desc": "The Void Orb hums with power.",
        "quest.3.desc": "A Lonely Phrase appears only once.",
    }
    cands = mine_candidates(entries, existing_terms=set())
    terms = {c.term for c in cands}
    assert "Void Orb" in terms
    assert "Lonely Phrase" not in terms
    void = next(c for c in cands if c.term == "Void Orb")
    assert void.count == 2
    assert len(void.contexts) == 2  # usage evidence for the curation prompt


def test_existing_terms_and_noise_are_excluded() -> None:
    entries = {
        # already covered by vanilla -> excluded (case-insensitive)
        "item.mymod.table": "Enchanting Table",
        # formatting codes stripped before matching
        "item.mymod.blade": "§6Frost Blade§r",
        "tooltip.mymod.blade": "The %s Frost Blade cuts {target}.",
    }
    cands = mine_candidates(entries, existing_terms={"enchanting table"})
    assert [c.term for c in cands] == ["Frost Blade"]
    assert cands[0].count == 2
    assert "§" not in cands[0].contexts[0]


def test_sentences_under_name_keys_are_not_names() -> None:
    cands = mine_candidates(
        {"item.mymod.tooltip": "Right-click to cast a spark."},
        existing_terms=set(),
    )
    assert cands == []


def test_ranking_and_cap() -> None:
    entries = {
        "item.mymod.a": "Alpha Gem",
        "quest.1": "Use the Beta Core. The Beta Core glows. Beta Core!",
        "quest.2": "Beta Core again and Gamma Stone here.",
        "quest.3": "Gamma Stone there.",
    }
    cands = mine_candidates(entries, existing_terms=set(), max_terms=2)
    # name-key first, then by corpus frequency.
    assert [c.term for c in cands] == ["Alpha Gem", "Beta Core"]


def test_unlimited_budget_keeps_every_ranked_candidate() -> None:
    entries = {
        "item.mymod.alpha": "Alpha Gem",
        "item.mymod.beta": "Beta Core",
        "item.mymod.gamma": "Gamma Stone",
    }

    limited = mine_candidates(entries, existing_terms=set(), max_terms=2)
    unlimited = mine_candidates(entries, existing_terms=set(), max_terms=None)

    assert [c.term for c in limited] == ["Alpha Gem", "Beta Core"]
    assert [c.term for c in unlimited] == [
        "Alpha Gem",
        "Beta Core",
        "Gamma Stone",
    ]


def test_deterministic_output() -> None:
    entries = {
        "quest.a": "Seek the Sunken Shrine.",
        "quest.b": "The Sunken Shrine sleeps.",
        "item.mymod.orb": "Void Orb",
    }
    first = mine_candidates(entries, existing_terms=set())
    second = mine_candidates(dict(reversed(entries.items())), existing_terms=set())
    assert [(c.term, c.count) for c in first] == [(c.term, c.count) for c in second]
