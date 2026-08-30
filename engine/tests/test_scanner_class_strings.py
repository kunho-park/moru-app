"""Display text a mod compiles into Java, where no lang file reaches it.

The scan reports these so an untranslated string in game is attributable
instead of looking like a moru failure. The bar for reporting is high on
purpose: a mod jar's constant pool is mostly logger messages, registry
ids and NBT keys, and a warning list that is usually wrong teaches users
to ignore the one signal that is right.

Fixtures build real class files byte by byte. A synthesized constant pool
is the only way to assert on the parser's actual contract — that a Long
consumes two pool slots, that only ``CONSTANT_String`` yields a literal —
without shipping a compiled jar into the repo.
"""

from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path

import pytest

from moru_engine.scanner.class_strings import (
    LangIndex,
    decode_mutf8,
    find_hardcoded_strings,
    own_packages,
    parse_constant_pool,
)

# --------------------------------------------------------------------------
# class-file construction


def _utf8(text: str) -> bytes:
    raw = text.encode("utf-8").replace(b"\x00", b"\xc0\x80")
    return bytes([1]) + struct.pack(">H", len(raw)) + raw


def _string(utf8_index: int) -> bytes:
    return bytes([8]) + struct.pack(">H", utf8_index)


def _class_entry(utf8_index: int) -> bytes:
    return bytes([7]) + struct.pack(">H", utf8_index)


def _name_and_type(name_index: int) -> bytes:
    return bytes([12]) + struct.pack(">HH", name_index, name_index)


def _methodref(class_index: int, nat_index: int) -> bytes:
    return bytes([10]) + struct.pack(">HH", class_index, nat_index)


def _long(value: int) -> bytes:
    return bytes([5]) + struct.pack(">q", value)


def _class_file(entries: list[bytes], *, tail: bytes = b"\x00\x21") -> bytes:
    """A class file whose pool holds ``entries``.

    ``tail`` stands in for access_flags onward; the parser must never read
    it, so its content is deliberately not a valid class body.
    """
    count = 1
    for entry in entries:
        count += 2 if entry[0] in (5, 6) else 1
    return (
        b"\xca\xfe\xba\xbe"
        + struct.pack(">HH", 0, 65)
        + struct.pack(">H", count)
        + b"".join(entries)
        + tail
    )


def _literal_class(*literals: str) -> bytes:
    """A class file whose only string literals are ``literals``."""
    entries: list[bytes] = []
    for offset, text in enumerate(literals):
        entries.append(_utf8(text))
        entries.append(_string(len(entries)))  # index of the utf8 just added
        del offset
    return _class_file(entries)


BOOK_PAGE = (
    '{"text":"Entry #12\\n\\nThe captain went down to the seabed at dawn '
    'and has not come back up."}'
)


def _mod_jar(
    path: Path,
    *,
    classes: dict[str, bytes] | None = None,
    lang: dict[str, str] | None = None,
    mod_id: str = "testmod",
    namespace: str = "testmod",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/mods.toml",
            f'modLoader="javafml"\n[[mods]]\nmodId="{mod_id}"\nversion="1.0"\n',
        )
        zf.writestr(
            f"assets/{namespace}/lang/en_us.json",
            json.dumps(lang if lang is not None else {"item.testmod.thing": "Thing"}),
        )
        for name, body in (classes or {}).items():
            zf.writestr(name, body)
    return path


def _find(path: Path, **kwargs):
    with zipfile.ZipFile(path) as zf:
        return find_hardcoded_strings(
            zf, jar_name=path.name, mod_id=kwargs.pop("mod_id", "testmod"), **kwargs
        )


# --------------------------------------------------------------------------
# constant pool parsing


def test_only_string_constants_become_literals():
    """A Utf8 entry is a literal only when a CONSTANT_String points at it.

    Every method name, field name and type descriptor also lives in the
    Utf8 pool. Harvesting those is what makes naive constant-pool mining
    unusable, so the distinction is the parser's core contract.
    """
    parsed = parse_constant_pool(
        _class_file(
            [
                _utf8("a real literal"),  # 1
                _utf8("getDisplayName"),  # 2 - a method name, never a literal
                _utf8("()Ljava/lang/String;"),  # 3 - a descriptor
                _string(1),  # 4
            ]
        )
    )
    assert parsed is not None
    assert parsed.literals == ["a real literal"]


def test_long_constant_consumes_two_pool_slots():
    """JVMS 4.4.5: a Long occupies two indices.

    Miscounting shifts every later index by one, which does not raise —
    it silently resolves string literals to the wrong Utf8 entries.
    """
    parsed = parse_constant_pool(
        _class_file(
            [
                _long(1234),  # 1 (and 2)
                _utf8("after the long"),  # 3
                _string(3),  # 4
            ]
        )
    )
    assert parsed is not None
    assert parsed.literals == ["after the long"]


def test_member_refs_resolve_to_owner_dot_member():
    parsed = parse_constant_pool(
        _class_file(
            [
                _utf8("net/minecraft/network/chat/Component"),  # 1
                _class_entry(1),  # 2
                _utf8("literal"),  # 3
                _name_and_type(3),  # 4
                _methodref(2, 4),  # 5
            ]
        )
    )
    assert parsed is not None
    assert "Component.literal" in parsed.refs


def test_parser_stops_before_access_flags():
    """Everything after the pool is unread, so garbage there is harmless."""
    parsed = parse_constant_pool(
        _class_file([_utf8("hello there"), _string(1)], tail=b"\xff" * 64)
    )
    assert parsed is not None
    assert parsed.literals == ["hello there"]


@pytest.mark.parametrize(
    ("label", "data"),
    [
        ("empty", b""),
        ("bad magic", b"PK\x03\x04" + b"\x00" * 32),
        ("truncated pool", b"\xca\xfe\xba\xbe" + struct.pack(">HHH", 0, 65, 40)),
        (
            "unknown tag",
            b"\xca\xfe\xba\xbe" + struct.pack(">HHH", 0, 65, 2) + bytes([99, 0, 0]),
        ),
    ],
)
def test_unparseable_input_returns_none(label, data):
    """Refusing to guess: a misparsed pool yields confident nonsense,
    while an unread class merely goes unreported."""
    assert parse_constant_pool(data) is None, label


def test_modified_utf8_null_is_decoded():
    assert decode_mutf8(b"a\xc0\x80b") == "a\x00b"
    assert decode_mutf8("책".encode()) == "책"


# --------------------------------------------------------------------------
# lang index: the three ways a literal legitimately matches the lang file


#: Verbatim from Occultism 1.20.1-1.158.0: the datagen literal in
#: ENUSProvider carries `%1$s`, the generated en_us.json carries the
#: substituted colour. Everything before that point is byte-identical.
_OCCULTISM_LANG = (
    "Familiar Rings consist of a [](item://occultism:soul_gem), that contains "
    "a [#](ad03fc)Djinni[#](), mounted on a ring."
)
_OCCULTISM_LITERAL = (
    "Familiar Rings consist of a [](item://occultism:soul_gem), that contains "
    "a [#](%1$s)Djinni[#](), mounted on a ring."
)


def test_lang_index_matches_exact_and_substring():
    lang = LangIndex(
        ["Simple Value", "Monitoring kinetic information using the Speedometer"]
    )
    assert "Simple Value" in lang
    assert "simple value" in lang
    # javac splits `"Monitoring … the " + name` into a fragment
    assert "Monitoring kinetic information using the" in lang
    assert "Something else entirely" not in lang


def test_lang_index_matches_across_a_substituted_placeholder():
    assert _OCCULTISM_LITERAL in LangIndex([_OCCULTISM_LANG])


def test_early_placeholder_leaves_too_little_to_match_on():
    """The rule is deliberately conservative.

    Only the span before the first substitution is comparable, so a
    literal that substitutes early has no stable head long enough to
    trust. That costs recall, never precision: the string is reported as
    a candidate and a human sees it, rather than being silently dropped
    on a 20-character coincidence.
    """
    lang = LangIndex(["Rings contain a spirit, mounted on a ring for the wearer"])
    assert "Rings contain a %1$s, mounted on a ring for the wearer" not in lang


def test_lang_index_substring_does_not_span_two_values():
    lang = LangIndex(["first value here", "second value here"])
    assert "value herefirst" not in lang


# --------------------------------------------------------------------------
# package attribution


def test_own_packages_covers_the_bulk_and_excludes_a_vendored_tail():
    names = [f"com/example/mod/C{i}.class" for i in range(20)]
    names += [f"blue/endless/jankson/J{i}.class" for i in range(5)]
    assert own_packages(names) == {"com/example/mod"}


# --------------------------------------------------------------------------
# end-to-end detection


def test_component_literal_is_reported(tmp_path):
    jar = _mod_jar(
        tmp_path / "mods" / "testmod-1.0.jar",
        classes={"com/example/testmod/Books.class": _literal_class(BOOK_PAGE)},
    )
    found = _find(jar)
    assert found is not None
    assert found.mod_id == "testmod"
    assert found.jar_name == "testmod-1.0.jar"
    assert [s.kind for s in found.strings] == ["component"]
    assert found.strings[0].text.startswith("Entry #12")
    assert found.strings[0].class_name == "com/example/testmod/Books.class"


def test_ordinary_mod_reports_nothing(tmp_path):
    """The 47-of-49 case. Logger messages, registry ids and NBT keys are
    what a normal jar is full of, and none of them may be reported."""
    jar = _mod_jar(
        tmp_path / "mods" / "plain-1.0.jar",
        classes={
            "com/example/testmod/Setup.class": _literal_class(
                "Failed to register {} for mod {}",
                "testmod:copper_ingot",
                "net/minecraft/world/item/Item",
                "item.testmod.thing",
                "textures/gui/widget.png",
                "pages",
            )
        },
    )
    assert _find(jar) is None


def test_string_already_in_lang_is_not_reported(tmp_path):
    """The same page shipped through a lang key is translatable, so it is
    not a finding — this is what separates two versions of one mod."""
    jar = _mod_jar(
        tmp_path / "mods" / "testmod-2.0.jar",
        classes={"com/example/testmod/Books.class": _literal_class(BOOK_PAGE)},
        lang={
            "book.testmod.page_1": (
                "Entry #12\n\nThe captain went down to the seabed at dawn "
                "and has not come back up."
            )
        },
    )
    assert _find(jar) is None


def test_translate_only_component_is_not_a_hardcode(tmp_path):
    """`{"translate": …}` is a *reference* to a lang key — the opposite of
    a hardcode, and the shape a mod migrates to when it fixes one."""
    jar = _mod_jar(
        tmp_path / "mods" / "testmod-3.0.jar",
        classes={
            "com/example/testmod/Books.class": _literal_class(
                '{"translate":"book.testmod.page_%s"}',
                '{"color":"dark_green","translate":"book.testmod.accent"}',
            )
        },
    )
    assert _find(jar) is None


def test_lang_provider_class_is_muted(tmp_path):
    """A datagen provider's literals *become* en_us.json. Reporting them
    is a pure false positive, and it is the highest-volume one there is:
    two mods in the sample corpus produced 123 wrong findings this way."""
    jar = _mod_jar(
        tmp_path / "mods" / "testmod-4.0.jar",
        classes={
            "com/example/testmod/data/ENUSProvider.class": _literal_class(BOOK_PAGE)
        },
    )
    assert _find(jar) is None


def test_vendored_package_is_muted(tmp_path):
    """Text belonging to a shaded library is not the pack author's to fix."""
    jar = _mod_jar(
        tmp_path / "mods" / "testmod-5.0.jar",
        classes={
            f"com/example/testmod/C{i}.class": _literal_class("item.testmod.thing")
            for i in range(10)
        }
        | {"blue/endless/jankson/Parser.class": _literal_class(BOOK_PAGE)},
    )
    assert _find(jar) is None


def test_associated_labels_ride_along_only_in_a_proven_class(tmp_path):
    """Guilt by association.

    "Pillagers Ship Logbook" is indistinguishable from an internal
    constant on its own, so it is trusted only inside a class already
    proven to hardcode display text. Single words stay out: they are
    ambiguous with the NBT keys sitting beside book text in that very
    class ("title", "author", "pages").
    """
    jar = _mod_jar(
        tmp_path / "mods" / "testmod-6.0.jar",
        classes={
            "com/example/testmod/Books.class": _literal_class(
                BOOK_PAGE,
                "Pillagers Ship Logbook",
                "title",
                "author",
                "pages",
                "Unknown",
                "item.testmod.logbook",
            ),
            # Same label, ordinary class: no component hit, no trust.
            "com/example/testmod/Registry.class": _literal_class(
                "Pillagers Outpost Logbook"
            ),
        },
    )
    found = _find(jar)
    assert found is not None
    texts = {s.text: s.kind for s in found.strings}
    assert texts.get("Pillagers Ship Logbook") == "associated"
    assert "Pillagers Outpost Logbook" not in texts
    for noise in ("title", "author", "pages", "Unknown", "item.testmod.logbook"):
        assert noise not in texts


def test_jar_without_classes_is_skipped(tmp_path):
    """Data-only mods are common and have nothing to parse."""
    jar = _mod_jar(tmp_path / "mods" / "datapack-1.0.jar")
    assert _find(jar) is None


def test_unparseable_class_does_not_sink_the_jar(tmp_path):
    """A finding is an extra, never a precondition: one bad entry must not
    cost the real hit in the next class."""
    jar = _mod_jar(
        tmp_path / "mods" / "testmod-7.0.jar",
        classes={
            "com/example/testmod/Broken.class": b"not a class file at all",
            "com/example/testmod/Books.class": _literal_class(BOOK_PAGE),
        },
    )
    found = _find(jar)
    assert found is not None
    assert found.count == 1
