"""Repeated strings are translated once, and only where that is safe.

A pack repeats the same short string across mods and files, and every
repeat is a paid model call. Two occurrences may share one translation
only when their dispatched text AND their context are the same, so dedup
is scoped to one ``pack_batches`` call — one file's translate wave — and
continuation runs are excluded outright.
"""

from __future__ import annotations

from moru_engine.batching import (
    continuation_groups,
    dedup_entries,
    expand_aliases,
    pack_batches,
)
from moru_engine.placeholder import PlaceholderProtector

TIP = "item.simplyswords.activedefencesworditem.tooltip"
TIP2 = f"{TIP}2"
TIP3 = f"{TIP}3"


# --- collapsing ----------------------------------------------------------


def test_repeated_text_collapses_onto_the_first_key() -> None:
    entries = {
        "attribute.mod.health": "Health",
        "gui.mod.health": "Health",
        "attribute.mod.speed": "Speed",
        "tooltip.mod.health": "Health",
    }
    unique, aliases = dedup_entries(entries)

    assert unique == {"attribute.mod.health": "Health", "attribute.mod.speed": "Speed"}
    assert aliases == {
        "gui.mod.health": "attribute.mod.health",
        "tooltip.mod.health": "attribute.mod.health",
    }


def test_unique_keeps_input_order_so_packing_is_unchanged() -> None:
    entries = {"c": "3", "a": "1", "b": "2", "d": "1"}
    unique, _ = dedup_entries(entries)

    assert list(unique) == ["c", "a", "b"]


def test_distinct_texts_are_left_alone() -> None:
    entries = {"a": "Health", "b": "Speed"}

    assert dedup_entries(entries) == (entries, {})


def test_empty_input_collapses_to_nothing() -> None:
    assert dedup_entries({}) == ({}, {})


def test_every_key_survives_as_unique_or_alias() -> None:
    entries = {f"k{i}": f"text {i % 3}" for i in range(9)}
    unique, aliases = dedup_entries(entries)

    assert set(unique) | set(aliases) == set(entries)
    assert not set(unique) & set(aliases)
    assert all(unique[rep] == entries[dup] for dup, rep in aliases.items())


# --- the scoping rule ----------------------------------------------------


def test_protection_makes_differently_coded_texts_share_one_call() -> None:
    # The pipeline dispatches PROTECTED text, and that is the stronger key:
    # "§6Gold" and "§aGold" differ only in a literal that cannot change the
    # words, so one call covers both and each key restores its own literal.
    protector = PlaceholderProtector()
    gold = protector.protect("§6Gold")
    green = protector.protect("§aGold")
    assert gold.protected == green.protected == "{{COLOR}}Gold"

    unique, aliases = dedup_entries(
        {"item.a.name": gold.protected, "item.b.name": green.protected}
    )
    assert list(unique) == ["item.a.name"]
    assert aliases == {"item.b.name": "item.a.name"}

    # One model output, two correct restorations.
    translated = expand_aliases({"item.a.name": "{{COLOR}}금"}, aliases)
    assert gold.restore(translated["item.a.name"]) == "§6금"
    assert green.restore(translated["item.b.name"]) == "§a금"


def test_a_continuation_run_member_is_never_merged_away() -> None:
    # TIP2's text duplicates a standalone entry, but a numbered run is read
    # as one sentence across display lines: line 2 of one run continues a
    # different clause than the identical line 2 of another.
    entries = {
        "gui.mod.hint": "Regularly fires arrows at nearby",
        TIP2: "Regularly fires arrows at nearby",
        TIP3: "enemies (Requires Arrows).",
    }
    unique, aliases = dedup_entries(entries)

    assert unique == entries
    assert aliases == {}


def test_a_run_member_never_becomes_a_representative() -> None:
    # Reverse order: the run member appears FIRST. It still must not lend
    # its translation to the standalone key.
    entries = {
        TIP2: "Deals bonus damage",
        TIP3: "to undead enemies.",
        "gui.mod.hint": "Deals bonus damage",
    }
    unique, aliases = dedup_entries(entries)

    assert unique == entries
    assert aliases == {}


def test_two_runs_sharing_a_line_stay_separate() -> None:
    other2 = "item.mod.other.tooltip2"
    other3 = "item.mod.other.tooltip3"
    entries = {
        TIP2: "Grants a shield when",
        TIP3: "you block an attack.",
        other2: "Grants a shield when",
        other3: "you are struck by lightning.",
    }
    unique, aliases = dedup_entries(entries)

    assert unique == entries
    assert aliases == {}


def test_dedup_leaves_continuation_groups_packable() -> None:
    entries = {
        "gui.mod.a": "Health",
        "gui.mod.b": "Health",
        TIP2: "Regularly fires arrows at nearby",
        TIP3: "enemies (Requires Arrows).",
    }
    unique, _ = dedup_entries(entries)

    assert continuation_groups(unique) == [[TIP2, TIP3]]
    # ...and the run still reaches one batch whole, in ordinal order.
    assert pack_batches(unique, batch_size=2) == [
        {"gui.mod.a": "Health"},
        {TIP2: entries[TIP2], TIP3: entries[TIP3]},
    ]


# --- fanning results back out --------------------------------------------


def test_expand_aliases_gives_each_occurrence_the_translation() -> None:
    aliases = {"gui.mod.health": "attribute.mod.health"}
    results = {"attribute.mod.health": "체력", "attribute.mod.speed": "속도"}

    assert expand_aliases(results, aliases) == {
        "attribute.mod.health": "체력",
        "attribute.mod.speed": "속도",
        "gui.mod.health": "체력",
    }


def test_expand_aliases_carries_failure_reasons_too() -> None:
    # Same function, list-valued results: an alias of a failed
    # representative must fail with it, not silently disappear.
    aliases = {"gui.mod.health": "attribute.mod.health"}
    failed = {"attribute.mod.health": ["placeholder mismatch"]}

    assert expand_aliases(failed, aliases) == {
        "attribute.mod.health": ["placeholder mismatch"],
        "gui.mod.health": ["placeholder mismatch"],
    }


def test_an_alias_of_a_missing_representative_stays_missing() -> None:
    # The model returned nothing for the representative: the alias must be
    # absent so the caller's "no translation returned" path handles both.
    aliases = {"gui.mod.health": "attribute.mod.health"}

    assert expand_aliases({"other": "값"}, aliases) == {"other": "값"}


def test_expand_aliases_does_not_mutate_its_input() -> None:
    results = {"a": "가"}
    expand_aliases(results, {"b": "a"})

    assert results == {"a": "가"}


# --- the saving ----------------------------------------------------------


def test_dedup_removes_whole_batches_from_the_dispatch() -> None:
    # 60 entries, 3 distinct strings: 2 batches of 30 become 1 batch of 3.
    entries = {f"quest.{i}.title": f"Objective {i % 3}" for i in range(60)}
    before = pack_batches(entries)
    unique, aliases = dedup_entries(entries)
    after = pack_batches(unique)

    assert len(before) == 2
    assert len(after) == 1
    assert sum(len(b) for b in after) == 3
    assert len(aliases) == 57
    # Every original key still ends up with a translation.
    dispatched = {key: "번역" for batch in after for key in batch}
    assert set(expand_aliases(dispatched, aliases)) == set(entries)
