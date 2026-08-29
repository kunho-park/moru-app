"""Key-scoped glossary rules: matching, precedence, rendering.

The defect these guard: vanilla "Wither" is the boss 위더 under
``entity.minecraft.wither`` and the status effect 시듦 under
``effect.minecraft.wither``, so an unscoped glossary carries both readings
of one source term and whichever rule the model picks is luck.
"""

from __future__ import annotations

import json
from pathlib import Path

from moru_engine.glossary.vanilla_builder import VanillaGlossaryBuilder
from moru_engine.models.glossary import UNSCOPED_RANK, Glossary, TermRule
from moru_engine.models.glossary_filter import GlossaryFilter


def _term(target: str, alias: str, scope: list[str] | None = None) -> TermRule:
    return TermRule(
        term_ko=target,
        preferred_style="용어 고정",
        aliases=[alias],
        key_scope=scope or [],
    )


def _targets(glossary: Glossary, key: str, text: str) -> list[str]:
    """Term targets the batch ``{key: text}`` actually receives."""
    filtered = GlossaryFilter.filter_for_texts(glossary, {key: text})
    return [rule.term_ko for rule in filtered.term_rules]


# -- scope matching -----------------------------------------------------------


def test_unscoped_rule_applies_to_every_key() -> None:
    rule = _term("위더", "Wither")
    assert rule.scope_rank("entity.minecraft.wither") == UNSCOPED_RANK
    assert rule.scope_rank("literally.anything") == UNSCOPED_RANK
    assert rule.scope_rank("") == UNSCOPED_RANK


def test_trailing_wildcard_absorbs_remaining_segments() -> None:
    rule = _term("시듦", "Wither", ["effect.*"])
    assert rule.scope_rank("effect.minecraft.wither") == (1, 2)
    # Modded effects live in the same namespace and must be covered too.
    assert rule.scope_rank("effect.somemod.deep.nested.key") == (1, 2)
    # Zero remaining segments still matches.
    assert rule.scope_rank("effect") == (1, 2)
    assert rule.scope_rank("entity.minecraft.wither") is None


def test_exact_key_scope_matches_only_that_key() -> None:
    rule = _term("시듦", "Wither", ["effect.minecraft.wither"])
    assert rule.scope_rank("effect.minecraft.wither") == (3, 3)
    assert rule.scope_rank("effect.minecraft.wither_skeleton") is None
    assert rule.scope_rank("effect.minecraft") is None
    assert rule.scope_rank("effect.minecraft.wither.extra") is None


def test_inner_wildcard_matches_exactly_one_segment() -> None:
    rule = _term("위더", "Wither", ["subtitles.*.wither"])
    assert rule.scope_rank("subtitles.entity.wither") == (2, 3)
    # One segment, not a tail: the deeper key does not match.
    assert rule.scope_rank("subtitles.entity.wither.death") is None
    assert rule.scope_rank("subtitles.wither") is None


def test_several_patterns_take_the_most_specific_match() -> None:
    rule = _term("위더", "Wither", ["entity.*", "entity.minecraft.wither"])
    assert rule.scope_rank("entity.minecraft.wither") == (3, 3)
    assert rule.scope_rank("entity.othermod.wither") == (1, 2)


def test_blank_patterns_are_dropped_and_scope_is_deduplicated() -> None:
    rule = _term("시듦", "Wither", ["  effect.*  ", "", "   ", "effect.*"])
    assert rule.key_scope == ["effect.*"]


# -- precedence ---------------------------------------------------------------


def test_scoped_rule_beats_unscoped_for_a_matching_key() -> None:
    glossary = Glossary(
        term_rules=[
            _term("시듦", "Wither", ["effect.*"]),
            _term("위더", "Wither"),
        ]
    )
    assert _targets(glossary, "effect.minecraft.wither", "Wither") == ["시듦"]


def test_unscoped_rule_still_serves_keys_no_scope_claims() -> None:
    glossary = Glossary(
        term_rules=[
            _term("시듦", "Wither", ["effect.*"]),
            _term("위더", "Wither"),
        ]
    )
    assert _targets(glossary, "entity.minecraft.wither", "Wither") == ["위더"]
    assert _targets(
        glossary, "enhanced_boss_bar.witherstormmod.witherstorm", "Wither Storm"
    ) == ["위더"]


def test_mixed_batch_keeps_both_readings() -> None:
    # Both keys ride in one batch, so both rules are relevant - each line
    # carries its scope so the model can apply them per key.
    glossary = Glossary(
        term_rules=[
            _term("시듦", "Wither", ["effect.*"]),
            _term("위더", "Wither"),
        ]
    )
    filtered = GlossaryFilter.filter_for_texts(
        glossary,
        {"effect.minecraft.wither": "Wither", "entity.minecraft.wither": "Wither"},
    )
    assert [rule.term_ko for rule in filtered.term_rules] == ["시듦", "위더"]


def test_two_scoped_rules_match_more_literal_segments_wins() -> None:
    glossary = Glossary(
        term_rules=[
            _term("네임스페이스", "Wither", ["effect.*"]),
            _term("정확한 키", "Wither", ["effect.minecraft.wither"]),
        ]
    )
    assert _targets(glossary, "effect.minecraft.wither", "Wither") == ["정확한 키"]
    # The broader rule still owns the keys the specific one does not claim.
    assert _targets(glossary, "effect.othermod.wither", "Wither") == ["네임스페이스"]


def test_two_scoped_rules_match_deeper_pattern_wins_on_equal_literals() -> None:
    # (1, 2) vs (1, 3): same literal count, more pattern segments wins.
    glossary = Glossary(
        term_rules=[
            _term("얕은 것", "Wither", ["effect.*"]),
            _term("깊은 것", "Wither", ["effect.*.*"]),
        ]
    )
    assert _targets(glossary, "effect.minecraft.wither", "Wither") == ["깊은 것"]


def test_fully_tied_scoped_rules_resolve_to_the_first_listed() -> None:
    # Both rank (2, 3); list order is the documented tie-break.
    glossary = Glossary(
        term_rules=[
            _term("먼저", "Wither", ["effect.*.wither"]),
            _term("나중", "Wither", ["*.minecraft.wither"]),
        ]
    )
    assert _targets(glossary, "effect.minecraft.wither", "Wither") == ["먼저"]


def test_scoped_rule_never_reaches_a_key_it_does_not_cover() -> None:
    # No competing rule at all: the scope alone must keep it out.
    glossary = Glossary(term_rules=[_term("시듦", "Wither", ["effect.*"])])
    assert _targets(glossary, "item.minecraft.wither_sword", "Wither Sword") == []
    assert _targets(glossary, "effect.minecraft.wither", "Wither") == ["시듦"]


def test_scope_resolution_is_per_key_not_per_batch() -> None:
    # The scoped key is in the batch, but the term appears only under a key
    # the scope does not cover - the scoped rule must not ride along.
    glossary = Glossary(term_rules=[_term("시듦", "Wither", ["effect.*"])])
    filtered = GlossaryFilter.filter_for_texts(
        glossary,
        {"effect.minecraft.wither": "Decay", "item.foo.bar": "Wither Blade"},
    )
    assert filtered.term_rules == []


def test_unrelated_terms_are_unaffected_by_a_scoped_neighbour() -> None:
    glossary = Glossary(
        term_rules=[
            _term("시듦", "Wither", ["effect.*"]),
            _term("주괴", "Ingot"),
        ]
    )
    assert _targets(glossary, "item.minecraft.iron_ingot", "Iron Ingot") == ["주괴"]


def test_rule_kept_by_an_uncontested_alias_survives_losing_a_scoped_one() -> None:
    # "Wither" is contested by a scoped rule, "Nether" is not; the
    # multi-alias rule must stay for the sake of the uncontested alias.
    multi = TermRule(
        term_ko="위더/네더",
        preferred_style="용어 고정",
        aliases=["Wither", "Nether"],
    )
    glossary = Glossary(term_rules=[_term("시듦", "Wither", ["effect.*"]), multi])
    assert _targets(glossary, "effect.minecraft.wither", "Wither Nether") == [
        "시듦",
        "위더/네더",
    ]


# -- rendering ----------------------------------------------------------------


def test_scope_is_rendered_with_a_header_instruction() -> None:
    glossary = Glossary(
        term_rules=[
            _term("시듦", "Wither", ["effect.*"]),
            _term("위더", "Wither"),
        ]
    )
    rendered = glossary.to_context_string()
    assert "(적용 키: effect.*)" in rendered
    assert "적용 키가 붙은 규칙은" in rendered
    # The default reading carries no scope marker.
    assert "- **Wither** → **위더** (스타일: 용어 고정)" in rendered


def test_rendering_is_unchanged_without_scopes() -> None:
    glossary = Glossary(term_rules=[_term("주괴", "Ingot")])
    assert glossary.to_context_string() == (
        "## Term Rules (MUST follow these translations)\n"
        "- **Ingot** → **주괴** (스타일: 용어 고정)"
    )


# -- backward compatibility ---------------------------------------------------

#: A glossary serialized before key_scope existed - no such key anywhere.
_LEGACY_DOC = {
    "term_rules": [
        {
            "term_ko": "시듦",
            "preferred_style": "Official Minecraft translation",
            "aliases": ["Wither"],
            "category": "effect",
            "notes": "From vanilla: effect.minecraft.wither",
        }
    ],
    "proper_noun_rules": [],
    "formatting_rules": [],
}


def test_term_rule_without_a_key_scope_key_loads() -> None:
    # The migration path: rows persisted before the field existed.
    payload = {
        "term_ko": "위더",
        "preferred_style": "용어 고정",
        "aliases": ["Wither"],
    }
    assert "key_scope" not in payload
    rule = TermRule.model_validate(payload)
    assert rule.key_scope == []
    assert rule.scope_rank("entity.minecraft.wither") == UNSCOPED_RANK


def test_legacy_serialized_glossary_loads_and_behaves_as_before() -> None:
    glossary = Glossary.model_validate_json(json.dumps(_LEGACY_DOC))
    rule = glossary.term_rules[0]
    assert rule.key_scope == []
    # Reaches every key, exactly as it did before scopes existed.
    assert _targets(glossary, "entity.minecraft.wither", "Wither") == ["시듦"]
    assert glossary.to_context_string() == (
        "## Term Rules (MUST follow these translations)\n"
        "- **Wither** → **시듦** (스타일: Official Minecraft translation)"
        " — From vanilla: effect.minecraft.wither"
    )


def test_merge_treats_scope_as_part_of_rule_identity() -> None:
    base = Glossary(term_rules=[_term("위더", "Wither", ["entity.*"])])
    merged = base.merge_with(
        Glossary(
            term_rules=[
                _term("위더", "Wither", ["entity.*"]),  # same rule -> dropped
                _term("위더", "Wither", ["item.*"]),  # other key space -> kept
            ]
        )
    )
    assert [rule.key_scope for rule in merged.term_rules] == [["entity.*"], ["item.*"]]


# -- vanilla builder ----------------------------------------------------------


def _builder() -> VanillaGlossaryBuilder:
    # Paths are only stored; _extract_terms does no I/O.
    return VanillaGlossaryBuilder(Path("en_us.json"), Path("ko_kr.json"))


def test_vanilla_wither_splits_into_scoped_effect_and_default_entity() -> None:
    source = {
        "effect.minecraft.wither": "Wither",
        "entity.minecraft.wither": "Wither",
        "block.minecraft.wither_rose": "Wither Rose",
    }
    target = {
        "effect.minecraft.wither": "시듦",
        "entity.minecraft.wither": "위더",
        "block.minecraft.wither_rose": "위더 장미",
    }
    scopes = {
        (rule.aliases[0], rule.term_ko): rule.key_scope
        for rule in _builder()._extract_terms(source, target)
    }
    # The narrow sense is scoped; the entity sense stays the default so
    # unclaimed keys (boss bars, advancements, mod keys) still get 위더.
    assert scopes[("Wither", "시듦")] == ["effect.*"]
    assert scopes[("Wither", "위더")] == []
    # Unambiguous terms are untouched.
    assert scopes[("Wither Rose", "위더 장미")] == []


def test_vanilla_same_namespace_conflict_falls_back_to_exact_keys() -> None:
    source = {"gui.all": "All", "gui.socialInteractions.tab_all": "All"}
    target = {"gui.all": "모두", "gui.socialInteractions.tab_all": "전체"}
    scopes = {
        rule.term_ko: rule.key_scope
        for rule in _builder()._extract_terms(source, target)
    }
    # One shared namespace cannot separate them, so the exact key does.
    assert scopes == {
        "모두": ["gui.all"],
        "전체": ["gui.socialInteractions.tab_all"],
    }


def test_vanilla_conflict_without_an_open_namespace_scopes_every_sense() -> None:
    source = {
        "effect.minecraft.speed": "Speed",
        "attribute.name.generic.movement_speed": "Speed",
    }
    target = {
        "effect.minecraft.speed": "속도 증가",
        "attribute.name.generic.movement_speed": "속도",
    }
    rules = _builder()._extract_terms(source, target)
    assert {rule.term_ko: rule.key_scope for rule in rules} == {
        "속도 증가": ["effect.*"],
        "속도": ["attribute.*"],
    }
    # Neither sense leaks onto arbitrary text mentioning "Speed".
    glossary = Glossary(term_rules=rules)
    assert _targets(glossary, "item.somemod.speed_boots", "Speed Boots") == []


# -- the reported case, end to end -------------------------------------------

#: A slice of the real vanilla lang pair around the "Wither" homograph.
_VANILLA_SOURCE = {
    "effect.minecraft.wither": "Wither",
    "entity.minecraft.wither": "Wither",
    "block.minecraft.wither_rose": "Wither Rose",
    "item.minecraft.wither_spawn_egg": "Wither Spawn Egg",
}
_VANILLA_TARGET = {
    "effect.minecraft.wither": "시듦",
    "entity.minecraft.wither": "위더",
    "block.minecraft.wither_rose": "위더 장미",
    "item.minecraft.wither_spawn_egg": "위더 생성 알",
}

_HEADER = "## Term Rules (MUST follow these translations)"
_SCOPE_HINT = (
    "(적용 키가 붙은 규칙은 그 키에만 적용하고, 나머지 키에는 "
    "적용 키 없는 규칙을 적용하세요)"
)
_EFFECT_LINE = (
    "- **Wither** → **시듦** (적용 키: effect.*) "
    "(스타일: Official Minecraft translation) "
    "— From vanilla: effect.minecraft.wither"
)
_ENTITY_LINE = (
    "- **Wither** → **위더** (스타일: Official Minecraft translation) "
    "— From vanilla: entity.minecraft.wither"
)


def _rendered(key: str, text: str) -> str:
    """Glossary text the batch {key: text} would actually be prompted with."""
    glossary = Glossary(
        term_rules=_builder()._extract_terms(_VANILLA_SOURCE, _VANILLA_TARGET)
    )
    return GlossaryFilter.filter_for_texts(glossary, {key: text}).to_context_string()


def test_effect_key_receives_only_the_status_effect_reading() -> None:
    assert _rendered("effect.minecraft.wither", "Wither") == "\n".join(
        [_HEADER, _SCOPE_HINT, _EFFECT_LINE]
    )


def test_entity_key_receives_only_the_boss_reading() -> None:
    assert _rendered("entity.minecraft.wither", "Wither") == "\n".join(
        [_HEADER, _ENTITY_LINE]
    )


def test_third_party_boss_key_receives_only_the_boss_reading() -> None:
    # The key from the bug report: enhanced_boss_bar.witherstormmod.witherstorm
    # = "Wither Storm". Before scoping, the effect rule could fire here and
    # produce "시듦 폭풍"; the effect rule must not reach a modded boss key.
    assert _rendered(
        "enhanced_boss_bar.witherstormmod.witherstorm", "Wither Storm"
    ) == "\n".join([_HEADER, _ENTITY_LINE])
