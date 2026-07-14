"""The {{KIND}} token roundtrip is sacred: protect -> (translate) -> restore.

Any mismatch must raise PlaceholderError — never pass silently.
"""

from __future__ import annotations

import pytest

from moru_engine.placeholder import TOKEN_RE, PlaceholderError, PlaceholderProtector


def test_protect_covers_all_token_kinds() -> None:
    text = "§6Hello §b%s§6! Use %1$s and {player} plus <b>bold</b>\\n done"
    protected = PlaceholderProtector().protect(text)
    assert "§" not in protected.protected
    assert "%s" not in protected.protected
    assert "{player}" not in protected.protected
    assert "<b>" not in protected.protected
    assert "\\n" not in protected.protected
    assert len(protected.placeholders) >= 7


def test_roundtrip_restores_original() -> None:
    text = "§aTip:§r Press %s to open the §bQuest Book§r!"
    protected = PlaceholderProtector().protect(text)
    # simulate translation: tokens kept, text replaced
    translated = protected.protected.replace("Press", "누르세요").replace(
        "to open the", "열려면"
    )
    restored = protected.restore(translated)
    for original in ("§a", "§r", "%s", "§b"):
        assert original in restored
    assert not TOKEN_RE.search(restored)


def test_missing_token_raises() -> None:
    protected = PlaceholderProtector().protect("Durability: %s / %s")
    tokens = [p.token for p in protected.placeholders]
    broken = protected.protected.replace(tokens[0], "")
    with pytest.raises(PlaceholderError):
        protected.restore(broken)


def test_leftover_unknown_token_raises() -> None:
    protected = PlaceholderProtector().protect("Level %s")
    with pytest.raises(PlaceholderError):
        protected.restore(protected.protected + " {{PH99}}")


def test_placeholder_only_detection() -> None:
    protector = PlaceholderProtector()
    only = protector.protect("§a%s§r")
    text = protector.protect("§aHello§r")
    assert protector.is_only_placeholders(only)
    assert not protector.is_only_placeholders(text)


def test_token_kinds_are_semantic() -> None:
    protected = PlaceholderProtector().protect(
        "§aHello§r %s and &6gold&r {player} <b>\\n"
    )
    kinds = {p.original: p.token for p in protected.placeholders}
    assert kinds["§a"].startswith("{{COLOR")
    assert kinds["§r"].startswith("{{RESET")
    assert kinds["&6"].startswith("{{COLOR")
    assert kinds["&r"].startswith("{{RESET")
    assert kinds["%s"].startswith("{{ARG")
    assert kinds["{player}"].startswith("{{VAR")
    assert kinds["<b>"].startswith("{{TAG")
    assert kinds["\\n"].startswith("{{BR")
    # roundtrip still exact
    assert (
        protected.restore(protected.protected)
        == "§aHello§r %s and &6gold&r {player} <b>\\n"
    )


def test_identical_literals_share_one_bare_token() -> None:
    # RESET and BR are always one literal -> never numbered; repeated %s
    # is one literal -> one bare token used twice.
    protected = PlaceholderProtector().protect("§6Gold§r %s / %s\\n§6More§r")
    tokens = {p.original: p.token for p in protected.placeholders}
    assert tokens["§6"] == "{{COLOR}}"
    assert tokens["§r"] == "{{RESET}}"
    assert tokens["%s"] == "{{ARG}}"
    assert tokens["\\n"] == "{{BR}}"
    assert protected.protected == (
        "{{COLOR}}Gold{{RESET}} {{ARG}} / {{ARG}}{{BR}}{{COLOR}}More{{RESET}}"
    )
    assert protected.restore(protected.protected) == "§6Gold§r %s / %s\\n§6More§r"


def test_distinct_literals_get_numbered_by_literal() -> None:
    # Two colors -> numbers identify WHICH color; repeats share the number.
    protected = PlaceholderProtector().protect("§6a§r §ab§r §6c")
    tokens = [(p.original, p.token) for p in sorted(protected.placeholders, key=lambda p: p.position)]
    assert tokens == [
        ("§6", "{{COLOR1}}"),
        ("§r", "{{RESET}}"),
        ("§a", "{{COLOR2}}"),
        ("§r", "{{RESET}}"),
        ("§6", "{{COLOR1}}"),
    ]
    assert protected.restore(protected.protected) == "§6a§r §ab§r §6c"


def test_reordered_tokens_restore_correct_literals() -> None:
    # Word-order changes move tokens; numbered tokens carry their literal.
    protected = PlaceholderProtector().protect("§6Gold§r and §aGreen§r")
    reordered = "{{COLOR2}}초록{{RESET}} 그리고 {{COLOR1}}금색{{RESET}}"
    assert protected.restore(reordered) == "§a초록§r 그리고 §6금색§r"


def test_partial_count_loss_raises() -> None:
    # Shared bare tokens are count-checked: dropping ONE of two {{ARG}}
    # occurrences must still fail the roundtrip.
    protected = PlaceholderProtector().protect("Durability: %s / %s")
    assert protected.protected.count("{{ARG}}") == 2
    with pytest.raises(PlaceholderError):
        protected.restore("내구도: {{ARG}}")


def test_surplus_known_token_raises() -> None:
    protected = PlaceholderProtector().protect("Level %s")
    with pytest.raises(PlaceholderError):
        protected.restore("레벨 {{ARG}} {{ARG}}")


def test_bracketed_prose_with_format_arg_roundtrips() -> None:
    # Regression: "<Error occurred, plz report to %s>" is prose, not a tag.
    # The old <[^>]+> pattern swallowed the whole sentence, and the nested
    # %s match shifted the outer span so restore produced "{{TAG}}RG}}>".
    text = "<Error occurred, plz report to %s>"
    protected = PlaceholderProtector().protect(text)
    assert protected.protected == "<Error occurred, plz report to {{ARG}}>"
    assert protected.restore(protected.protected) == text


def test_real_tags_still_protected() -> None:
    text = "<b>bold</b> and <color=red>red"
    protected = PlaceholderProtector().protect(text)
    assert sorted(p.token for p in protected.placeholders) == [
        "{{TAG1}}",
        "{{TAG2}}",
        "{{TAG3}}",
    ]
    assert protected.restore(protected.protected) == text


def test_overlapping_matches_keep_earlier_pattern() -> None:
    # "<%s>" is both a tag-shaped span and a nested format arg: the
    # earlier pattern (java_format) wins and the overlapping tag match
    # is dropped, so restore stays a clean literal replacement.
    text = "<%s>"
    protected = PlaceholderProtector().protect(text)
    assert protected.protected == "<{{ARG}}>"
    assert protected.restore(protected.protected) == text


def test_attribute_tags_with_spaces_stay_protected() -> None:
    # Whitespace inside a tag is fine when it separates name=value
    # attributes (or a spaced self-close) — only bare prose is rejected.
    text = '<font color="red">hot</font> <a href=\'https://moru.gg\'>link</a><br />'
    protected = PlaceholderProtector().protect(text)
    tag_literals = {
        p.original for p in protected.placeholders if p.token.startswith("{{TAG")
    }
    assert tag_literals == {
        '<font color="red">',
        "</font>",
        "<a href='https://moru.gg'>",
        "</a>",
        "<br />",
    }
    assert protected.restore(protected.protected) == text


def test_valueless_words_after_tag_name_read_as_prose() -> None:
    # "<Error occurred plz report>" has no =value attributes: bare words
    # after the first are prose, not attributes, so nothing is frozen.
    protected = PlaceholderProtector().protect("<Error occurred plz report>")
    assert protected.placeholders == []
