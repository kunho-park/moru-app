"""Batch packing: continuation runs stay whole, key sets stay intact."""

from __future__ import annotations

from moru_engine.batching import continuation_groups, pack_batches

#: Real-world case: simplyswords splits one sentence over two tooltip keys.
TIP = "item.simplyswords.activedefencesworditem.tooltip"
TIP2 = f"{TIP}2"
TIP3 = f"{TIP}3"
LINE2 = "Regularly fires arrows at nearby"
LINE3 = "enemies (Requires Arrows)."


# --- grouping ------------------------------------------------------------


def test_numbered_tooltip_siblings_form_one_ordered_group() -> None:
    # File order is not ordinal order here; the group must still read 2 -> 3.
    assert continuation_groups({TIP3: LINE3, TIP2: LINE2}) == [[TIP2, TIP3]]


def test_multi_digit_ordinals_sort_numerically() -> None:
    keys = [f"{TIP}2", f"{TIP}10", f"{TIP}9"]
    assert continuation_groups(keys) == [[f"{TIP}2", f"{TIP}9", f"{TIP}10"]]


def test_bracketed_ordinals_group_too() -> None:
    keys = ["quests[0].description[1]", "quests[0].description[0]"]
    assert continuation_groups(keys) == [
        ["quests[0].description[0]", "quests[0].description[1]"]
    ]


def test_lone_and_unnumbered_keys_belong_to_no_group() -> None:
    assert continuation_groups(["item.mod.sword", f"{TIP}1"]) == []


def test_separator_variants_stay_in_separate_groups() -> None:
    # ``desc1`` and ``desc.1`` are different key shapes, not one run.
    assert continuation_groups(["a.desc1", "a.desc2", "a.desc.1", "a.desc.2"]) == [
        ["a.desc1", "a.desc2"],
        ["a.desc.1", "a.desc.2"],
    ]


def test_different_stems_do_not_merge() -> None:
    keys = [TIP2, TIP3, "item.mod.other.tooltip2", "item.mod.other.tooltip3"]
    assert continuation_groups(keys) == [
        [TIP2, TIP3],
        ["item.mod.other.tooltip2", "item.mod.other.tooltip3"],
    ]


# --- packing -------------------------------------------------------------


def test_entry_limit_no_longer_splits_a_run() -> None:
    entries = {"gui.mod.title": "Active Defence", TIP2: LINE2, TIP3: LINE3}
    # Greedy packing by entry count would cut between the two tooltip lines.
    assert pack_batches(entries, batch_size=2) == [
        {"gui.mod.title": "Active Defence"},
        {TIP2: LINE2, TIP3: LINE3},
    ]


def test_char_limit_no_longer_splits_a_run() -> None:
    entries = {"gui.mod.title": "x" * 40, TIP2: "y" * 30, TIP3: "z" * 30}
    assert pack_batches(entries, max_batch_chars=80) == [
        {"gui.mod.title": "x" * 40},
        {TIP2: "y" * 30, TIP3: "z" * 30},
    ]


def test_run_reaches_its_batch_in_ordinal_order() -> None:
    (batch,) = pack_batches({TIP3: LINE3, TIP2: LINE2})
    assert list(batch) == [TIP2, TIP3]


def test_scattered_run_members_are_pulled_together() -> None:
    entries = {"item.mod.a": "A", TIP2: LINE2, "item.mod.b": "B", TIP3: LINE3}
    batches = pack_batches(entries, batch_size=3)
    assert list(batches[0]) == ["item.mod.a", TIP2, TIP3]
    assert batches[1] == {"item.mod.b": "B"}


def test_key_set_and_values_survive_packing_exactly() -> None:
    entries = {
        "item.mod.a": "A",
        TIP2: LINE2,
        "item.mod.b": "B",
        TIP3: LINE3,
        f"{TIP}4": "Cooldown applies.",
    }
    batches = pack_batches(entries, batch_size=2)
    packed = [key for batch in batches for key in batch]
    # No key dropped, duplicated, or altered.
    assert len(packed) == len(entries)
    assert sorted(packed) == sorted(entries)
    assert all(
        batch[key] == entries[key] for batch in batches for key in batch
    )


def test_run_too_large_for_one_batch_is_chunked_in_order() -> None:
    entries = {f"{TIP}{i}": f"line {i}" for i in range(1, 6)}
    assert [list(batch) for batch in pack_batches(entries, batch_size=2)] == [
        [f"{TIP}1", f"{TIP}2"],
        [f"{TIP}3", f"{TIP}4"],
        [f"{TIP}5"],
    ]


def test_plain_entries_keep_the_original_greedy_packing() -> None:
    entries = {name: "x" * 10 for name in ("alpha", "beta", "gamma", "delta")}
    expected = [
        {"alpha": "x" * 10, "beta": "x" * 10},
        {"gamma": "x" * 10, "delta": "x" * 10},
    ]
    assert pack_batches(entries, batch_size=2) == expected
    assert pack_batches(entries, max_batch_chars=25) == expected


def test_single_oversized_entry_still_forms_its_own_batch() -> None:
    assert pack_batches({"alpha": "x" * 100, "beta": "y"}, max_batch_chars=10) == [
        {"alpha": "x" * 100},
        {"beta": "y"},
    ]


def test_empty_input_packs_to_nothing() -> None:
    assert pack_batches({}) == []
