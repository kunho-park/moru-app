"""Patchouli ``$(...)`` macros must survive a translation untouched.

Guidebook text is full of colour/format/link macros. Handed to the model
as prose they get reworded, reordered or corrupted, and Patchouli renders
``[ERROR]`` for a macro it cannot parse. The protection pattern is
byte-identical to the game's own ``BookTextParser.COMMAND_PATTERN``
(``\\$\\(([^)]*)\\)``), so what the engine freezes is exactly what the game
treats as one atomic command.
"""

from __future__ import annotations

import pytest

from moru_engine.placeholder import (
    TOKEN_RE,
    PlaceholderError,
    PlaceholderProtector,
)

#: Verbatim page text from a real book in the sample pack
#: (patchouli_books/almanac/en_us/entries/tree_crops/pawpaw.json, pages[0].text).
REAL_PAGE = (
    "$(l)Stats$()$(li):coin: 80$(li)🍂 §4Autumn§r$(li)Yield: 1"
    "$(li)Grow stages: 8$(br2)$(l)Notes$()$(li)See: "
    "$(l:trees/pams/pawpaw)Pawpaw Tree$(/l)"
)


@pytest.fixture
def protector() -> PlaceholderProtector:
    return PlaceholderProtector()


# --- roundtrip -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Every distinct macro shape observed across 269 real en_us books.
        "$()",
        "$(li)",
        "$(l)",
        "$(br)",
        "$(br2)",
        "$(/l)",
        "$(0)",
        "$(p)",
        "$(o)",
        "$(bold)",
        "$(clear)",
        "$(item)Enchanting Table$()",
        "$(thing)Prismatic Shard$()",
        "See: $(l:trees/pams/pawpaw)Pawpaw Tree$(/l)",
        "See: $(l:buildinggadgets2:copypaste)Copy Paste$(/l)",
        # Shapes the game's parser also accepts.
        "Press $(k:inventory) to open your inventory.",
        "$(#b0b)purple$() and $(#490)green$()",
        "$(t:Hover me)tooltip$(/t)",
        "$(li2)nested bullet",
        "Hello $(playername)!",
        "$(c:/give @s stone)run$(/c)",
        REAL_PAGE,
    ],
)
def test_macro_text_roundtrips_byte_identical(
    protector: PlaceholderProtector, text: str
) -> None:
    protected = protector.protect(text)
    assert "$(" not in protected.protected
    assert protected.restore(protected.protected) == text


def test_a_real_page_survives_a_translation(protector: PlaceholderProtector) -> None:
    protected = protector.protect(REAL_PAGE)
    assert protected.protected == (
        "{{COLOR1}}Stats{{RESET1}}{{BR1}}:coin: 80{{BR1}}🍂 {{COLOR2}}Autumn"
        "{{RESET2}}{{BR1}}Yield: 1{{BR1}}Grow stages: 8{{BR2}}{{COLOR1}}Notes"
        "{{RESET1}}{{BR1}}See: {{TAG1}}Pawpaw Tree{{TAG2}}"
    )
    translated = (
        protected.protected.replace("Stats", "능력치")
        .replace("Notes", "참고")
        .replace("Yield", "수확량")
        .replace("Grow stages", "성장 단계")
        .replace("See:", "참조:")
        .replace("Pawpaw Tree", "포포나무")
        .replace("Autumn", "가을")
    )

    restored = protected.restore(translated)
    assert restored == (
        "$(l)능력치$()$(li):coin: 80$(li)🍂 §4가을§r$(li)수확량: 1"
        "$(li)성장 단계: 8$(br2)$(l)참고$()$(li)참조: "
        "$(l:trees/pams/pawpaw)포포나무$(/l)"
    )
    assert not TOKEN_RE.search(restored)


def test_a_dropped_macro_fails_the_roundtrip(
    protector: PlaceholderProtector,
) -> None:
    protected = protector.protect("$(li)First$(li)Second")
    assert protected.protected.count("{{BR}}") == 2
    with pytest.raises(PlaceholderError):
        protected.restore("첫째{{BR}}둘째")


def test_macro_only_text_is_placeholder_only(
    protector: PlaceholderProtector,
) -> None:
    # A page fragment of pure markup needs no model call at all.
    assert protector.is_only_placeholders(protector.protect("$(br2)$(li)"))
    assert not protector.is_only_placeholders(protector.protect("$(li)Yield: 1"))


# --- semantic kinds ------------------------------------------------------


@pytest.mark.parametrize(
    ("macro", "kind"),
    [
        # Breaks and list bullets end the line.
        ("$(br)", "BR"),
        ("$(br2)", "BR"),
        ("$(2br)", "BR"),
        ("$(p)", "BR"),
        ("$(li)", "BR"),
        ("$(li2)", "BR"),
        # Style resets mirror "§r".
        ("$()", "RESET"),
        ("$(reset)", "RESET"),
        ("$(clear)", "RESET"),
        ("$(nocolor)", "RESET"),
        # Wrapping markup, like "<a href=...>".."</a>".
        ("$(l:entry/path)", "TAG"),
        ("$(/l)", "TAG"),
        ("$(t:Hover me)", "TAG"),
        ("$(/t)", "TAG"),
        ("$(c:/give @s stone)", "TAG"),
        ("$(/c)", "TAG"),
        # Values substituted at render time, like "{player}".
        ("$(k:inventory)", "VAR"),
        ("$(playername)", "VAR"),
        # Colour/format spans, including book-defined macros.
        ("$(l)", "COLOR"),
        ("$(o)", "COLOR"),
        ("$(m)", "COLOR"),
        ("$(n)", "COLOR"),
        ("$(k)", "COLOR"),
        ("$(bold)", "COLOR"),
        ("$(italics)", "COLOR"),
        ("$(0)", "COLOR"),
        ("$(f)", "COLOR"),
        ("$(#b0b)", "COLOR"),
        ("$(#ff0000)", "COLOR"),
        ("$(item)", "COLOR"),
        ("$(thing)", "COLOR"),
    ],
)
def test_macro_kinds_reuse_the_documented_vocabulary(
    protector: PlaceholderProtector, macro: str, kind: str
) -> None:
    # The translator prompt documents COLOR/RESET/ARG/VAR/TAG/BR and no
    # others, so a patchouli macro must map onto one of those.
    (placeholder,) = protector.protect(f"x{macro}y").placeholders
    assert placeholder.token == f"{{{{{kind}}}}}"


def test_bold_l_and_link_l_are_told_apart(
    protector: PlaceholderProtector,
) -> None:
    # "$(l)" is bold but "$(l:target)" is a link: the parameter form is the
    # only difference, and they must not share a token.
    protected = protector.protect("$(l)Bold$() $(l:page)Link$(/l)")
    tokens = {p.original: p.token for p in protected.placeholders}
    assert tokens["$(l)"] == "{{COLOR}}"
    assert tokens["$(l:page)"] == "{{TAG1}}"
    assert tokens["$(/l)"] == "{{TAG2}}"
    assert protected.restore(protected.protected) == "$(l)Bold$() $(l:page)Link$(/l)"


def test_repeated_identical_macros_share_one_bare_token(
    protector: PlaceholderProtector,
) -> None:
    protected = protector.protect("$(li)a$(li)b$(li)c")
    assert protected.protected == "{{BR}}a{{BR}}b{{BR}}c"


# --- overlap priority ----------------------------------------------------


def test_a_macro_is_atomic_and_wins_over_nested_matches(
    protector: PlaceholderProtector,
) -> None:
    # The game parses "$(...)" as ONE command, so a "%s" or "{...}" inside
    # it is macro syntax, not a placeholder of its own. Letting the nested
    # match win would drop the macro and expose its "$(" and ")".
    protected = protector.protect("$(t:Press %s now)Go$(/t)")
    literals = [p.original for p in protected.placeholders]
    assert "$(t:Press %s now)" in literals
    assert "%s" not in literals
    assert protected.restore(protected.protected) == "$(t:Press %s now)Go$(/t)"


def test_a_bare_dollar_is_not_a_macro(protector: PlaceholderProtector) -> None:
    assert protector.protect("Costs $5 per trade").placeholders == []
    assert protector.protect("100$ reward").placeholders == []


def test_an_unterminated_macro_is_left_as_prose(
    protector: PlaceholderProtector,
) -> None:
    assert protector.protect("$(l unterminated").placeholders == []


# --- the existing %s collapse behaviour, alongside macros ----------------


def test_collapsed_positional_args_still_resolve_next_to_macros(
    protector: PlaceholderProtector,
) -> None:
    protected = protector.protect("$(li)Added %s to %s for %s (now %s)")
    assert protected.protected == (
        "{{BR}}Added {{ARG}} to {{ARG}} for {{ARG}} (now {{ARG}})"
    )
    assert protected.collapsed_positional_arg() == ("{{ARG}}", "%s", 4)

    reordered = "{{BR}}{{ARG3}}의 {{ARG2}}에 {{ARG1}}을(를) 더했습니다 (이제 {{ARG4}}입니다)"
    assert protected.restore(reordered) == (
        "$(li)%3$s의 %2$s에 %1$s을(를) 더했습니다 (이제 %4$s입니다)"
    )


def test_bare_shared_args_still_restore_unnumbered_next_to_macros(
    protector: PlaceholderProtector,
) -> None:
    text = "$(li)Durability: %s / %s$(br)"
    protected = protector.protect(text)
    assert protected.restore(protected.protected) == text


def test_a_macro_does_not_become_a_positional_arg(
    protector: PlaceholderProtector,
) -> None:
    # Only unindexed java specifiers collapse; macros never classify as ARG.
    protected = protector.protect("$(li)a$(li)b")
    assert protected.collapsed_positional_arg() is None
