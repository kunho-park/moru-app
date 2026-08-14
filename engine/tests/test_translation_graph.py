"""Unit coverage for the in-memory translation relationship graph."""

from __future__ import annotations

import pytest

from moru_engine.graph import TranslationGraph, is_name_entry, stem
from moru_engine.models import Glossary, ProperNounRule, TermRule


# -- term detection -----------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "source"),
    [
        ("item.testmod.void_orb", "Void Orb"),
        ("block.testmod.echo_slab", "Echo Slab"),
        ("entity.testmod.warden", "Warden"),
        # Formatting codes are cleaned before the name check.
        ("item.testmod.fancy", "§6Void §rOrb"),
        ("tooltip.item.testmod.thing.name", "Ancient Relic"),
    ],
)
def test_is_name_entry_accepts_name_keys(key: str, source: str) -> None:
    assert is_name_entry(key, source)


@pytest.mark.parametrize(
    ("key", "source"),
    [
        # No name-key prefix.
        ("quests[0].title", "Void Orb"),
        ("gui.testmod.greeting", "Void Orb"),
        # Sentences under a name key are tooltips, not names.
        ("item.testmod.void_orb.tooltip", "Grants a wish, once."),
        # Too short after cleanup to anchor a term.
        ("item.testmod.ok", "Ok"),
        # Placeholder-only value cleans to nothing.
        ("item.testmod.count", "%s"),
        ("item.testmod.param", "{name}"),
    ],
)
def test_is_name_entry_rejects_non_names(key: str, source: str) -> None:
    assert not is_name_entry(key, source)


# -- sibling stems ------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        # jsonseg suffix and array index are peeled before the field check.
        ("quests[0].description[3]::jsonseg[2]", "quests[0]"),
        ("quests[0].description[3]", "quests[0]"),
        ("quests[0].title", "quests[0]"),
        # Lang-style field suffix.
        ("item.mod.x.tooltip", "item.mod.x"),
        # Non-field last segment: the key is its own stem, so the name
        # entry lands in the same group as its tooltip.
        ("item.mod.x", "item.mod.x"),
        # Single segment, no dots.
        ("title", "title"),
        # Field check is case-insensitive.
        ("quest.intro.Description", "quest.intro"),
    ],
)
def test_stem(key: str, expected: str) -> None:
    assert stem(key) == expected


# -- fixtures -----------------------------------------------------------------


def build_graph() -> TranslationGraph:
    """Lang file defines two names; a quest file mentions one of them."""
    return TranslationGraph.build(
        [
            (
                "kubejs/assets/testmod/lang/en_us.json",
                {
                    "item.testmod.void_orb": "Void Orb",
                    "item.testmod.void_orb.tooltip": "Hums quietly.",
                    "block.testmod.lone_slab": "Lone Slab",
                },
                {"item.testmod.void_orb": "공허의 보주"},
            ),
            (
                "config/ftbquests/quests/chapters/intro.snbt",
                {
                    "quests[0].title": "First Steps",
                    "quests[0].description[0]": "Collect the Void Orb.",
                    "quests[0].description[1]": "It hums with void orbit energy.",
                },
                {"quests[0].title": "첫 걸음"},
            ),
        ]
    )


# -- mentions -----------------------------------------------------------------


def test_mentions_are_word_bounded_and_case_insensitive() -> None:
    graph = build_graph()
    stats = graph.stats()
    assert stats["entries"] == 6
    assert stats["terms"] == 2  # Void Orb, Lone Slab

    # "Collect the Void Orb." mentions it (case-insensitively lowered);
    # "void orbit energy" must NOT match: "orbit" is not word-bounded "orb".
    base = Glossary()
    rules = graph.bindings(base)
    assert [rule.aliases for rule in rules] == [["Void Orb"]]


def test_binding_requires_a_mention() -> None:
    # "Lone Slab" is defined but never mentioned elsewhere: no rule, even
    # though a translation could settle later.
    graph = build_graph()
    graph.record_translation(
        "kubejs/assets/testmod/lang/en_us.json",
        "block.testmod.lone_slab",
        "외로운 반암",
    )
    rules = graph.bindings(Glossary())
    assert all(rule.aliases != ["Lone Slab"] for rule in rules)


# -- bindings -----------------------------------------------------------------


def test_binding_uses_known_translation_and_category() -> None:
    rules = build_graph().bindings(Glossary())
    assert len(rules) == 1
    rule = rules[0]
    assert rule.term_ko == "공허의 보주"
    assert rule.aliases == ["Void Orb"]
    assert rule.category == "item"
    assert rule.preferred_style == "용어 고정"
    assert rule.notes == ""


def test_binding_skips_untranslated_copy() -> None:
    graph = TranslationGraph.build(
        [
            (
                "lang/en_us.json",
                {"item.mod.thing": "Shiny Thing"},
                {"item.mod.thing": "Shiny  Thing"},  # source copy, not a translation
            ),
            ("quests.snbt", {"q.text": "Bring the Shiny Thing"}, {}),
        ]
    )
    assert graph.bindings(Glossary()) == []


def test_binding_defers_to_base_glossary_aliases() -> None:
    graph = build_graph()
    base_term = Glossary(
        term_rules=[
            TermRule(
                term_ko="보이드 오브",
                preferred_style="용어 고정",
                aliases=["void orb"],
            )
        ]
    )
    assert graph.bindings(base_term) == []

    base_noun = Glossary(
        proper_noun_rules=[
            ProperNounRule(source_like="Void Orb", preferred_ko="보이드 오브")
        ]
    )
    assert graph.bindings(base_noun) == []


def test_binding_conflict_resolves_to_most_frequent_then_min_key() -> None:
    # Majority wins.
    graph = TranslationGraph.build(
        [
            ("a/lang.json", {"item.m.gem": "Aurora Gem"}, {"item.m.gem": "오로라 보석"}),
            ("b/lang.json", {"item.m.gem": "Aurora Gem"}, {"item.m.gem": "여명 보석"}),
            ("c/lang.json", {"item.m.gem": "Aurora Gem"}, {"item.m.gem": "오로라 보석"}),
            ("quests.snbt", {"q.text": "Find the Aurora Gem"}, {}),
        ]
    )
    (rule,) = graph.bindings(Glossary())
    assert rule.term_ko == "오로라 보석"

    # Tie: value of the smallest (file, key) wins.
    graph = TranslationGraph.build(
        [
            ("b/lang.json", {"item.m.gem": "Aurora Gem"}, {"item.m.gem": "여명 보석"}),
            ("a/lang.json", {"item.m.gem": "Aurora Gem"}, {"item.m.gem": "오로라 보석"}),
            ("quests.snbt", {"q.text": "Find the Aurora Gem"}, {}),
        ]
    )
    (rule,) = graph.bindings(Glossary())
    assert rule.term_ko == "오로라 보석"


def test_record_translation_feeds_bindings() -> None:
    graph = TranslationGraph.build(
        [
            ("lang/en_us.json", {"item.mod.core": "Ember Core"}, {}),
            ("quests.snbt", {"q.text": "Craft an Ember Core"}, {}),
        ]
    )
    assert graph.bindings(Glossary()) == []
    graph.record_translation("lang/en_us.json", "item.mod.core", "잉걸불 핵")
    (rule,) = graph.bindings(Glossary())
    assert rule.term_ko == "잉걸불 핵"
    # Unknown nodes are ignored, not created.
    graph.record_translation("lang/en_us.json", "item.mod.ghost", "유령")
    assert len(graph.bindings(Glossary())) == 1


# -- from_entries -------------------------------------------------------------


def test_from_entries_matches_build() -> None:
    class Entry:
        def __init__(
            self, file: str, key: str, source_text: str, translated_text: str | None
        ) -> None:
            self.file = file
            self.key = key
            self.source_text = source_text
            self.translated_text = translated_text

    entries = [
        Entry(
            "kubejs/assets/testmod/lang/en_us.json",
            "item.testmod.void_orb",
            "Void Orb",
            "공허의 보주",
        ),
        Entry(
            "kubejs/assets/testmod/lang/en_us.json",
            "item.testmod.void_orb.tooltip",
            "Hums quietly.",
            None,
        ),
        Entry(
            "kubejs/assets/testmod/lang/en_us.json",
            "block.testmod.lone_slab",
            "Lone Slab",
            None,
        ),
        Entry(
            "config/ftbquests/quests/chapters/intro.snbt",
            "quests[0].title",
            "First Steps",
            "첫 걸음",
        ),
        Entry(
            "config/ftbquests/quests/chapters/intro.snbt",
            "quests[0].description[0]",
            "Collect the Void Orb.",
            None,
        ),
        Entry(
            "config/ftbquests/quests/chapters/intro.snbt",
            "quests[0].description[1]",
            "It hums with void orbit energy.",
            None,
        ),
    ]
    rebuilt = TranslationGraph.from_entries(entries)
    reference = build_graph()
    assert rebuilt.stats() == reference.stats()
    assert [r.model_dump() for r in rebuilt.bindings(Glossary())] == [
        r.model_dump() for r in reference.bindings(Glossary())
    ]


# -- sibling context ----------------------------------------------------------


def test_sibling_context_format_exclusion_and_order() -> None:
    graph = build_graph()
    rel = "config/ftbquests/quests/chapters/intro.snbt"
    batch = ["quests[0].description[0]", "quests[0].description[1]"]
    block = graph.sibling_context(rel, batch, exclude=set(batch))
    # Only the translated title qualifies; untranslated siblings are silent.
    assert block == '- quests[0].title: "First Steps" => "첫 걸음"'

    # The batch itself is excluded even when translated.
    graph.record_translation(rel, "quests[0].description[0]", "공허의 보주를 모으세요.")
    block = graph.sibling_context(rel, batch, exclude=set(batch))
    assert "description[0]" not in block

    # A different batch in the group now sees both settled siblings.
    block = graph.sibling_context(
        rel, ["quests[0].description[1]"], exclude={"quests[0].description[1]"}
    )
    assert block.splitlines() == [
        '- quests[0].title: "First Steps" => "첫 걸음"',
        '- quests[0].description[0]: "Collect the Void Orb." => "공허의 보주를 모으세요."',
    ]

    # No group / no siblings -> empty string.
    assert graph.sibling_context(rel, ["nope"], exclude=set()) == ""
    assert (
        graph.sibling_context(
            "kubejs/assets/testmod/lang/en_us.json",
            ["block.testmod.lone_slab"],
            exclude={"block.testmod.lone_slab"},
        )
        == ""
    )


def test_sibling_context_line_and_char_caps() -> None:
    source = {"group.title": "Long Title"}
    known = {"group.title": "긴 제목"}
    for i in range(20):
        source[f"group.description[{i}]"] = f"Line {i} " + "x" * 150
        known[f"group.description[{i}]"] = f"라인 {i} " + "y" * 150
    graph = TranslationGraph.build([("file.snbt", source, known)])

    block = graph.sibling_context(
        "file.snbt", ["group.title"], exclude={"group.title"}, max_lines=3
    )
    assert len(block.splitlines()) == 3
    # Fields are flattened and truncated to 100 chars inside quotes.
    first = block.splitlines()[0]
    assert '=> "' in first
    assert max(len(part) for part in first.split('"')) <= 100

    block = graph.sibling_context(
        "file.snbt",
        ["group.title"],
        exclude={"group.title"},
        max_lines=50,
        max_chars=500,
    )
    assert block
    assert len(block) <= 500 + len(block.splitlines()[-1])
    assert len(block.splitlines()) < 20


def test_sibling_context_newlines_flattened() -> None:
    graph = TranslationGraph.build(
        [
            (
                "file.snbt",
                {"q.title": "Multi\nLine Title", "q.text": "Body"},
                {"q.title": "여러\n줄 제목"},
            )
        ]
    )
    block = graph.sibling_context("file.snbt", ["q.text"], exclude={"q.text"})
    assert block == '- q.title: "Multi Line Title" => "여러 줄 제목"'
