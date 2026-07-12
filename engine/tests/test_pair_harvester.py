"""Unit coverage for harvesting mods' own target-locale translations."""

from __future__ import annotations

from moru_engine.glossary.pair_harvester import (
    TranslatedTerm,
    build_term_rules,
    collect_translated_terms,
    is_untranslated_copy,
)


def test_name_key_pairs_become_terms() -> None:
    source = {
        "item.testmod.storm_hammer": "Storm Hammer",
        "block.testmod.arcane_stone": "Arcane Stone",
        "gui.testmod.settings": "Settings",  # not a name key
    }
    target = {
        "item.testmod.storm_hammer": "폭풍 망치",
        "block.testmod.arcane_stone": "비전 석재",
        "gui.testmod.settings": "설정",
    }
    terms = collect_translated_terms(source, target)
    assert {(t.source, t.target, t.category) for t in terms} == {
        ("Storm Hammer", "폭풍 망치", "item"),
        ("Arcane Stone", "비전 석재", "block"),
    }


def test_cjk_targets_survive_locale_agnostic_checks() -> None:
    # The Latin-script noun-phrase check applies to the SOURCE side only;
    # CJK (or any non-Latin) targets must never be filtered out.
    source = {"item.a.b": "Iron Ingot", "entity.a.c": "Iron Golem"}
    target = {"item.a.b": "鉄インゴット", "entity.a.c": "철 골렘"}
    assert len(collect_translated_terms(source, target)) == 2


def test_untranslated_copies_are_not_harvested() -> None:
    # Mods routinely copy en_us into other locale files wholesale or leave
    # part of the entries untouched; identical values carry no signal.
    source = {
        "item.a.copied": "Copper Gear",
        "item.a.recolored": "§6Copper Ingot",
        "item.a.translated": "Copper Wand",
    }
    target = {
        "item.a.copied": "Copper Gear",
        "item.a.recolored": "copper ingot",
        "item.a.translated": "구리 지팡이",
    }
    terms = collect_translated_terms(source, target)
    assert [(t.source, t.target) for t in terms] == [("Copper Wand", "구리 지팡이")]


def test_fully_copied_file_yields_no_terms() -> None:
    source = {"item.a.b": "Copper Gear", "block.a.c": "Arcane Stone"}
    assert collect_translated_terms(source, dict(source)) == []


def test_missing_placeholder_and_sentence_values_are_skipped() -> None:
    source = {
        "item.a.missing": "Void Orb",
        "item.a.placeholder": "%s Press",
        "item.a.tooltip": "Right-click to cast a spell.",
    }
    target = {
        "item.a.placeholder": "%s 프레스",
        "item.a.tooltip": "우클릭으로 주문을 시전합니다.",
    }
    assert collect_translated_terms(source, target) == []


def test_formatting_codes_are_stripped_from_terms() -> None:
    source = {"item.a.b": "§6Storm Hammer§r"}
    target = {"item.a.b": "§6폭풍 망치§r"}
    terms = collect_translated_terms(source, target)
    assert [(t.source, t.target) for t in terms] == [("Storm Hammer", "폭풍 망치")]


def test_unanimous_sources_become_one_rule() -> None:
    terms = [
        TranslatedTerm("Storm Hammer", "폭풍 망치", "item"),
        TranslatedTerm("storm hammer", "폭풍 망치", "item"),  # casefold-merged
    ]
    rules = build_term_rules(terms)
    assert len(rules) == 1
    rule = rules[0]
    assert rule.term_ko == "폭풍 망치"
    assert rule.aliases == ["Storm Hammer"]
    assert rule.category == "item"
    assert rule.notes == "existing mod translation"


def test_conflicting_sources_are_dropped_entirely() -> None:
    # Mod A and mod B disagree; forcing either choice onto the other's
    # context would be wrong, so the source produces NO global rule even
    # when one variant has the majority.
    terms = [
        TranslatedTerm("Tank", "탱크", "block"),
        TranslatedTerm("Tank", "탱크", "block"),
        TranslatedTerm("Tank", "수조", "block"),
        TranslatedTerm("Gear", "기어", "item"),
    ]
    rules = build_term_rules(terms)
    assert [rule.aliases for rule in rules] == [["Gear"]]


def test_known_aliases_always_win() -> None:
    terms = [TranslatedTerm("Iron Ingot", "철괴", "item")]
    assert build_term_rules(terms, {"iron ingot"}) == []
    assert build_term_rules(terms, {"Iron Ingot"}) == []


def test_rules_are_input_order_independent() -> None:
    # Alias casing / category tie-breaks must not depend on scan order:
    # the glossary fingerprint keys the TM cache.
    terms = [
        TranslatedTerm("Iron Ingot", "철 주괴", "item"),
        TranslatedTerm("iron ingot", "철 주괴", "item"),
        TranslatedTerm("Arcane Stone", "비전 석재", "block"),
    ]
    forward = build_term_rules(terms)
    reverse = build_term_rules(list(reversed(terms)))
    assert forward == reverse
    assert [rule.aliases[0] for rule in forward] == ["Arcane Stone", "Iron Ingot"]


def test_is_untranslated_copy_detects_filler() -> None:
    assert is_untranslated_copy("Iron Ingot", "Iron Ingot")
    assert is_untranslated_copy("Iron Ingot", "iron ingot")
    assert is_untranslated_copy("Iron Ingot", "§6Iron Ingot§r")
    assert is_untranslated_copy("Iron Ingot", "   ")
    assert is_untranslated_copy("Iron Ingot", "")
    assert not is_untranslated_copy("Iron Ingot", "철 주괴")
