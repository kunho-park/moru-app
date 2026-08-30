"""Key-scoped glossary rules: matching, precedence, rendering.

The defect these guard: vanilla "Wither" is the boss 위더 under
``entity.minecraft.wither`` and the status effect 시듦 under
``effect.minecraft.wither``, so an unscoped glossary carries both readings
of one source term and whichever rule the model picks is luck.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import moru_engine
import pytest
from moru_engine.glossary.vanilla_builder import VanillaGlossaryBuilder
from moru_engine.models.glossary import (
    UNSCOPED_RANK,
    Glossary,
    TermRule,
    is_key_scope_pattern,
)
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


# -- the derived dataset, pinned ----------------------------------------------

#: The real vanilla lang pair every shipped rule is derived from.
_LANG_DIR = (
    Path(moru_engine.__file__).parent
    / "assets"
    / "vanilla_minecraft_assets"
    / "versions"
    / "1.21.5"
)

#: moru-web bundles its OWN copy of the derived dataset: its admin import
#: writes those rows into the database, the snapshot publisher serves them and
#: the desktop syncs that snapshot. Nothing regenerates the copy, so a builder
#: change lands in the engine and silently leaves the copy users actually read
#: one revision behind. The repos sit side by side.
_WEB_DATASET = (
    Path(__file__).resolve().parents[3]
    / "moru-web"
    / "src"
    / "data"
    / "vanilla-glossary-ko_kr.json"
)

#: Identity of the derived dataset. The digests pin all 6425 rows without
#: pasting them; the counts and the Wither pair are asserted separately so a
#: failure names the property that broke instead of only "a byte moved".
#: These same two literals are asserted by moru-web against its copy, which is
#: what ties the two repos together — a dataset regenerated here cannot go
#: green there until the copy is refreshed.
_TOTAL_RULES = 6425
_SCOPED_RULES = 38
_SEQUENCE_SHA = "0529579a40f96aea9c24931ac5d0ddceb629e5341b859bfdbdf2589090c6fe1a"
_SCOPED_SHA = "d024d3e48cdc986d6d7ea1e9d3434f855633ba5b7499c8703fe2d2bc1fe3b364"

_Row = tuple[str, str, str, list[str]]


def _digest(rows: list[_Row]) -> str:
    """Hash of the rows in a form any language can reproduce byte for byte."""
    canonical = "\n".join(
        "\0".join((source, target, category, ",".join(scope)))
        for source, target, category, scope in rows
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _lang_pair() -> tuple[dict[str, str], dict[str, str]]:
    return (
        json.loads((_LANG_DIR / "en_us.json").read_text(encoding="utf-8")),
        json.loads((_LANG_DIR / "ko_kr.json").read_text(encoding="utf-8")),
    )


def _built_rows() -> list[_Row]:
    """The shipped dataset, rebuilt from the bundled lang files."""
    source, target = _lang_pair()
    return [
        (
            rule.aliases[0] if rule.aliases else "",
            rule.term_ko,
            rule.category,
            list(rule.key_scope),
        )
        for rule in _builder()._extract_terms(source, target)
    ]


def test_derived_dataset_matches_its_pinned_identity() -> None:
    """Scoping is a NARROWING, so the counts are load-bearing in both
    directions: 38 scoped rules keep the homographs apart, and the other 6387
    staying unscoped is what keeps thousands of correct rules firing. A
    derivation that scoped the bulk would fix the bug and cause a far larger
    one, so the totals and the exact scoped set are pinned, not just the size.
    """
    rows = _built_rows()
    assert len(rows) == _TOTAL_RULES
    scoped = [row for row in rows if row[3]]
    assert len(scoped) == _SCOPED_RULES
    assert _digest(scoped) == _SCOPED_SHA
    assert _digest(rows) == _SEQUENCE_SHA


def test_derived_dataset_keeps_both_wither_senses() -> None:
    """The reported bug, asserted against the real corpus rather than a slice."""
    wither = {
        (target, tuple(scope))
        for source, target, _category, scope in _built_rows()
        if source == "Wither"
    }
    assert wither == {("시듦", ("effect.*",)), ("위더", ())}


def test_target_dedup_never_collapses_a_homograph_group() -> None:
    """The builder drops any pair whose Korean text was already claimed
    (``_extract_terms`` L201-207). That dedup is keyed on the TARGET, while a
    homograph group is defined by a shared SOURCE, so members cannot evict one
    another — but a *different* source term claiming one of those Korean
    strings first would delete one sense before ``_scope_ambiguous`` ever sees
    the group. The group would then look unambiguous, stay unscoped, and ship
    a partially scoped homograph that still reads as a successful build.

    Nothing in the code prevents that; it happens not to occur in this corpus.
    So assert it over the real lang files, where a future version bump would
    otherwise introduce it silently.
    """
    source, target = _lang_pair()
    accepted: dict[str, set[str]] = {}
    for key, text in source.items():
        rendered = target.get(key)
        if not rendered or rendered == text:
            continue
        accepted.setdefault(text.lower(), set()).add(rendered)

    emitted: dict[str, int] = {}
    for row_source, _t, _c, _k in _built_rows():
        emitted[row_source.lower()] = emitted.get(row_source.lower(), 0) + 1

    collapsed = {
        text: {"distinct_targets": len(targets), "rules_emitted": emitted.get(text, 0)}
        for text, targets in accepted.items()
        if len(targets) > 1 and emitted.get(text, 0) != len(targets)
    }
    assert collapsed == {}


def test_web_copy_of_the_dataset_has_not_drifted() -> None:
    """The copy in moru-web is what a user's app actually ends up reading, and
    it is refreshed by hand. Compare it to a fresh build so the two cannot part
    ways unnoticed; skipped when the sibling repo simply is not checked out.
    """
    if not _WEB_DATASET.is_file():
        pytest.skip(f"sibling repo not present: {_WEB_DATASET}")
    web: list[dict[str, object]] = json.loads(_WEB_DATASET.read_text(encoding="utf-8"))
    rows: list[_Row] = [
        (
            str(entry["source"]),
            str(entry["target"]),
            str(entry["category"]),
            [str(pattern) for pattern in entry["key_scope"]],  # type: ignore[union-attr]
        )
        for entry in web
    ]
    assert len(rows) == _TOTAL_RULES
    assert _digest(rows) == _SEQUENCE_SHA
    assert _digest([row for row in rows if row[3]]) == _SCOPED_SHA


# -- pattern grammar ----------------------------------------------------------


def test_well_formed_patterns_are_accepted() -> None:
    for pattern in (
        "effect.*",
        "effect.minecraft.wither",
        "subtitles.*.wither",
        "*",
        "itemGroup.coloredBlocks",
        "some-mod.thing_two.*",
    ):
        assert is_key_scope_pattern(pattern), pattern


def test_a_pattern_that_could_never_fire_is_rejected() -> None:
    """The separator-mismatch shape is the one that matters.

    ``scope_rank`` returns None for these on every key, which is exactly what
    an honest non-match returns, so nothing downstream can flag them. Without
    a grammar check they are stored, displayed and edited as real rules while
    applying to nothing.
    """
    dead = "effect.*;status_effect.*"
    assert is_key_scope_pattern(dead) is False
    # Confirm the trap: indistinguishable from a legitimate miss.
    assert _term("시듦", "Wither", [dead]).scope_rank("effect.minecraft.wither") is None
    for pattern in ("effect.*, entity.*", "effect..wither", ".effect", "effect.", ""):
        assert is_key_scope_pattern(pattern) is False, pattern


def test_the_grammar_accepts_every_shipped_scope_pattern() -> None:
    """Whatever the builder derives must survive the boundary it is sent
    through — a rule the engine emits but its own validator would reject
    would make the vanilla dataset unsavable.
    """
    for _s, _t, _c, scope in _built_rows():
        for pattern in scope:
            assert is_key_scope_pattern(pattern), pattern
