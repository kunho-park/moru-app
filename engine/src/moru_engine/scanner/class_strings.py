"""Display strings a mod hardcodes in Java, where no lang file can reach.

Some mods build player-facing text with ``Component.literal("…")`` instead
of routing it through ``assets/<ns>/lang/en_us.json``. Those strings are
untranslatable by *any* language-file tool — ours, a hand translation, or
a resource pack — because the game never consults a lang key for them.
Users reasonably read the untranslated result as a moru bug, so the scan
names them instead of leaving the user to hunt.

What this module reports is deliberately narrow. A mod jar's constant
pool holds thousands of strings and almost all of them are logger
messages, registry ids, NBT keys and resource paths; a prose heuristic
over that soup measures ~14% precision, and a warning list that is wrong
six times in seven teaches users to ignore the one signal that is right.
So only two shapes are reported:

``component``
    The literal parses as a serialized text component (``{"text": …}``,
    ``{"extra": […]}``). Mojang's own wire format for displayable text —
    a logger message cannot be shaped like this. Measured 22/22 across 49
    real jars, with zero hits in the 47 that do not hardcode.

``associated``
    A multi-word literal in a class that already produced a ``component``
    hit. The class is proven to hardcode display text, so its short
    Title Case labels (book titles, boss names) are display text too.
    Those labels are indistinguishable from internal constants on their
    own, which is why they are only trusted inside a proven class.

Three exclusions carry the precision and none is optional:

* Only ``CONSTANT_String`` entries are harvested. Every string in a class
  file lives in the ``CONSTANT_Utf8`` pool, including method names, field
  names and type descriptors; ``CONSTANT_String`` is emitted only for a
  literal that appeared in the source.
* Lang *providers* are muted. A datagen class like ``ENUSProvider`` holds
  book text as literals, but that literal is the source that generates
  ``en_us.json`` — reporting it is a pure false positive. This rule alone
  removed 123 wrong findings from two mods in the sample corpus.
* A candidate is dropped if the mod's own ``en_us`` already contains it,
  matched three ways: exactly, as a substring (javac splits ``"a " + x``
  into fragments), and by placeholder-normalised prefix (a provider's
  ``%1$s`` is a substituted value by the time it reaches the lang file).
"""

from __future__ import annotations

import json
import logging
import re
import struct
import zipfile
from collections import Counter
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

U2 = struct.Struct(">H")

_MAGIC = b"\xca\xfe\xba\xbe"

#: Bytes following the tag byte, per ``CONSTANT_*`` tag (JVMS 4.4).
#: ``CONSTANT_Utf8`` (1) is absent: it is length-prefixed, not fixed.
_FIXED: dict[int, int] = {
    3: 4,  # Integer
    4: 4,  # Float
    5: 8,  # Long                (consumes two pool indices)
    6: 8,  # Double              (consumes two pool indices)
    7: 2,  # Class
    8: 2,  # String
    9: 4,  # Fieldref
    10: 4,  # Methodref
    11: 4,  # InterfaceMethodref
    12: 4,  # NameAndType
    15: 3,  # MethodHandle
    16: 2,  # MethodType
    17: 4,  # Dynamic
    18: 4,  # InvokeDynamic
    19: 2,  # Module
    20: 2,  # Package
}

#: JVMS 4.4.5: a Long or Double occupies *two* constant-pool slots. Miss
#: this and every index after the first one is off by one, which yields
#: plausible-looking garbage rather than an error.
_WIDE = frozenset({5, 6})


def decode_mutf8(raw: bytes) -> str:
    """Decode a ``CONSTANT_Utf8`` payload.

    Java's modified UTF-8 differs from the real thing in two places: NUL
    is encoded ``C0 80``, and astral characters use CESU-8 surrogate
    pairs. The first is corrected exactly; the second is rare enough in
    display text to absorb with replacement characters.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.replace(b"\xc0\x80", b"\x00").decode("utf-8", "replace")


@dataclass(slots=True)
class ClassConstants:
    """The parts of one class file's constant pool we act on."""

    #: Values of ``CONSTANT_Utf8`` entries referenced by a
    #: ``CONSTANT_String`` — i.e. string literals written in the source.
    literals: list[str] = field(default_factory=list)
    #: ``Owner.member`` for every Field/Method/InterfaceMethodref, used to
    #: tell what kind of class this is without decoding any bytecode.
    refs: set[str] = field(default_factory=set)


def parse_constant_pool(data: bytes) -> ClassConstants | None:
    """Read a class file's header and constant pool; nothing further.

    Returns ``None`` for anything that is not a class file we fully
    understand — truncated data, a bad magic number, an unknown tag.
    Refusing to guess is deliberate: a misparsed pool produces confident
    nonsense, and a mod we silently skip merely goes unreported.
    """
    if len(data) < 10 or data[:4] != _MAGIC:
        return None

    # 0:4 magic, 4:6 minor, 6:8 major (unread — the pool format is stable
    # across every class-file version), 8:10 constant_pool_count.
    count = U2.unpack_from(data, 8)[0]
    end = len(data)
    pos = 10

    utf8: dict[int, str] = {}
    string_refs: list[int] = []
    class_names: dict[int, int] = {}
    name_and_types: dict[int, int] = {}
    members: list[tuple[int, int]] = []

    # Valid indices are 1..count-1, so the pool holds count-1 entries.
    index = 1
    while index < count:
        if pos >= end:
            return None
        tag = data[pos]
        pos += 1

        if tag == 1:
            if pos + 2 > end:
                return None
            length = U2.unpack_from(data, pos)[0]
            pos += 2
            if pos + length > end:
                return None
            utf8[index] = decode_mutf8(data[pos : pos + length])
            pos += length
        else:
            size = _FIXED.get(tag)
            if size is None or pos + size > end:
                return None
            if tag == 8:
                string_refs.append(U2.unpack_from(data, pos)[0])
            elif tag == 7:
                class_names[index] = U2.unpack_from(data, pos)[0]
            elif tag == 12:
                name_and_types[index] = U2.unpack_from(data, pos)[0]
            elif tag in (9, 10, 11):
                members.append(
                    (U2.unpack_from(data, pos)[0], U2.unpack_from(data, pos + 2)[0])
                )
            pos += size
            if tag in _WIDE:
                index += 1
        index += 1

    # `pos` now points at access_flags. Methods, fields and the Code
    # attribute are never read: the literals are all in the pool, and
    # skipping the rest is most of why this is fast enough to always run.
    result = ClassConstants()
    result.literals = [utf8[ref] for ref in string_refs if ref in utf8]
    for class_index, nat_index in members:
        owner = utf8.get(class_names.get(class_index, -1), "")
        member = utf8.get(name_and_types.get(nat_index, -1), "")
        if owner and member:
            result.refs.add(f"{owner.rsplit('/', 1)[-1]}.{member}")
    return result


# --------------------------------------------------------------------------
# class-level context


#: Build-time language and book generators. Their literals *become* the
#: shipped ``en_us.json``, so they are the translatable path, not a
#: hardcode. Occultism's ``ENUSProvider`` and Malum's ``MalumLang`` hold
#: whole guidebooks this way.
_LANG_PROVIDER_CLASS = re.compile(
    r"(Lang(uage)?(Provider|Gen\w*)?|ENUSProvider|[A-Z]\w*Lang|BookProvider"
    r"|\w*TranslationProvider)$"
)
_LANG_PROVIDER_REFS = (
    "LanguageProvider.add",
    "LanguageProvider.<init>",
    "RegistrateLangProvider",
    "LangProvider.add",
    "AbstractLanguageProvider",
    "TranslationBuilder.add",
)

#: Third-party code vendored into the jar. Its strings belong to the
#: upstream library, not to the mod, and are never player-facing text the
#: pack author can act on.
_VENDOR_PACKAGE = re.compile(
    r"^(org/(yaml|apache|slf4j|objectweb|antlr|joml|checkerframework|jetbrains"
    r"|intellij|reactivestreams|jline|graalvm|w3c|xml|json)"
    r"|com/(google|mojang|electronwill|fasterxml|typesafe|sun|ibm)"
    r"|io/(netty|github/classgraph)|kotlin|kotlinx|scala|joptsimple|it/unimi"
    r"|blue/endless|net/jpountz|javax|jakarta)"
    r"|/(shadow\w*|shaded|repack\w*|libs?|vendor)/",
    re.IGNORECASE,
)


def own_packages(
    class_names: list[str], depth: int = 3, cover: float = 0.6
) -> set[str]:
    """The mod's own package roots: the fewest ``depth``-segment prefixes
    covering ``cover`` of its classes.

    Shading defeats a fixed denylist — cloth-config relocates Jankson
    under its *own* root — so the mod's real home is derived from where
    the bulk of its classes actually live.
    """
    counts: Counter[str] = Counter()
    for name in class_names:
        segments = name.split("/")[:-1]
        if segments:
            counts["/".join(segments[:depth])] += 1
    total = sum(counts.values())
    if total == 0:
        return set()

    keep: set[str] = set()
    seen = 0
    for package, hits in counts.most_common():
        keep.add(package)
        seen += hits
        if seen / total >= cover:
            break
    return keep


def _is_foreign(name: str, own: set[str]) -> bool:
    if _VENDOR_PACKAGE.search(name):
        return True
    return bool(own) and not any(name.startswith(f"{package}/") for package in own)


def _is_lang_provider(name: str, refs: set[str]) -> bool:
    declaring = name[:-6].rsplit("/", 1)[-1] if name.endswith(".class") else name
    if _LANG_PROVIDER_CLASS.search(declaring):
        return True
    return any(marker in ref for ref in refs for marker in _LANG_PROVIDER_REFS)


# --------------------------------------------------------------------------
# the mod's own English, indexed for the three ways a literal matches it


_PLACEHOLDER = re.compile(r"%\d*\$?[sdfx]|\{\}|\x01")
_WHITESPACE = re.compile(r"\s+")
_PREFIX_LEN = 40
_SUBSTRING_MIN = 12


class LangIndex:
    """Membership over one mod's ``en_us`` values.

    Exact equality is not enough, because a literal legitimately differs
    from the string it produces in two ways:

    *substring* — javac splits ``"Total: " + n + " items"`` into separate
    literals, so a fragment matches no value but sits inside one.

    *prefix* — a datagen literal carries ``%1$s`` where the generated lang
    value carries the substituted text. Everything *before* the first
    substitution is identical on both sides, so that head is the part
    that can be compared; anything after it has already diverged.
    """

    __slots__ = ("_exact", "_blob", "_prefixes")

    def __init__(self, values: list[str]) -> None:
        normalized = [value.strip().lower() for value in values]
        self._exact = set(normalized)
        # NUL joins the values so a candidate cannot match across two.
        self._blob = "\x00".join(normalized)
        self._prefixes = {
            collapsed[:_PREFIX_LEN]
            for value in values
            if len(collapsed := _collapse(value)) >= _PREFIX_LEN
        }

    def __contains__(self, text: str) -> bool:
        lowered = text.strip().lower()
        if lowered in self._exact:
            return True
        if len(lowered) >= _SUBSTRING_MIN and lowered in self._blob:
            return True
        head = _stable_head(text)
        return len(head) >= _PREFIX_LEN and head[:_PREFIX_LEN] in self._prefixes


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text.strip().lower()).strip()


def _stable_head(text: str) -> str:
    """The literal up to its first placeholder — the span that survives
    substitution unchanged, and therefore the only span worth comparing."""
    collapsed = _collapse(text)
    match = _PLACEHOLDER.search(collapsed)
    return collapsed[: match.start()] if match else collapsed


_LANG_ENTRY = re.compile(r"^assets/([\w.\-]+)/lang/(?P<locale>[a-z]{2}_[a-z]{2})\.json$")


def source_lang_values(zf: zipfile.ZipFile, locale: str) -> list[str]:
    """Every string value in the jar's own source-locale lang files."""
    values: list[str] = []
    for name in zf.namelist():
        match = _LANG_ENTRY.match(name)
        if match is None or match.group("locale") != locale:
            continue
        try:
            parsed = json.loads(zf.read(name).decode("utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, KeyError):
            continue
        if isinstance(parsed, dict):
            values.extend(v for v in parsed.values() if isinstance(v, str))
    return values


# --------------------------------------------------------------------------
# candidate classification


#: Keys that identify a JSON object as a Minecraft text component.
_COMPONENT_KEYS = frozenset(
    {
        "text",
        "extra",
        "translate",
        "color",
        "italic",
        "bold",
        "underlined",
        "obfuscated",
        "strikethrough",
        "font",
        "hoverEvent",
        "clickEvent",
        "score",
        "selector",
        "keybind",
    }
)

#: ``"text":"…"`` payloads recovered from a component literal that will
#: not parse as JSON — a concatenation template or an embedded control
#: character. Keeps the English, drops the scaffolding.
_PARTIAL_TEXT = re.compile(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"')

_WORD = re.compile(r"[A-Za-z][A-Za-z'\u2019\-]*")

#: Shapes that are never display text, used for `associated` literals.
_RESOURCE_ID = re.compile(r"^[a-z0-9_.\-/]+:[a-z0-9_.\-/]+$")
_DOTTED_ID = re.compile(r"^[A-Za-z_$][\w$]*(\.[\w$]+)+$")
_FILE_PATH = re.compile(
    r"^[\w./\-]+\.(json|png|toml|txt|nbt|ogg|mcmeta|class|java|zip|jar|lang"
    r"|properties|cfg|md|yml|yaml|xml|obj|vsh|fsh|glsl)$",
    re.IGNORECASE,
)
_DESCRIPTOR = re.compile(r"^\(.*\)[BCDFIJSZVL\[]|^L[\w/$]+;$")
_URL = re.compile(r"^(https?|file|jar|mailto|jdbc):|^www\.", re.IGNORECASE)
#: Two or more path segments, e.g. `net/minecraft/world`.
_JVM_PATH = re.compile(r"[\w$]+/[\w$]+/[\w$]+")

_MIN_COMPONENT_WORDS = 3
_MIN_ASSOCIATED_WORDS = 2


def _parse_component(text: str) -> object | None:
    """The literal as a parsed text component, ``"partial"`` when it is a
    recognisable but unparseable fragment, or ``None`` when it is not one.
    """
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return None
    if not any(
        marker in stripped for marker in ('"text"', '"extra"', '"translate"')
    ):
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return "partial"

    def component_keys(value: object) -> bool:
        if isinstance(value, dict):
            return bool(_COMPONENT_KEYS & set(value))
        if isinstance(value, list) and value:
            return component_keys(value[0])
        return False

    return parsed if component_keys(parsed) else None


def component_text(value: object) -> str:
    """Flatten a parsed component to the English it displays."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(component_text(item) for item in value)
    if isinstance(value, dict):
        out = value.get("text", "")
        text = out if isinstance(out, str) else ""
        extra = value.get("extra")
        if isinstance(extra, list):
            text += "".join(component_text(item) for item in extra)
        hover = value.get("hoverEvent")
        if isinstance(hover, dict):
            contents = hover.get("contents", hover.get("value"))
            if contents is not None:
                text += " " + component_text(contents)
        return text
    return ""


def _component_hit(literal: str, lang: LangIndex) -> str | None:
    """The display English of a hardcoded component literal, if it is one.

    A component whose only content is ``translate`` carries no English of
    its own — it is a *reference* to a lang key, the exact opposite of a
    hardcode — and falls out here on the word count.
    """
    parsed = _parse_component(literal)
    if parsed is None:
        return None
    text = (
        "".join(_PARTIAL_TEXT.findall(literal))
        if parsed == "partial"
        else component_text(parsed)
    ).strip()
    if len(_WORD.findall(text)) < _MIN_COMPONENT_WORDS:
        return None
    if text in lang:
        return None
    return text


def _associated_hit(literal: str, lang: LangIndex) -> str | None:
    """A display label riding along in a class already proven to hardcode.

    Only multi-word text qualifies. A single word is ambiguous with the
    NBT keys and registry names that sit beside book text in the same
    class (``title``, ``author``, ``pages``), and no amount of context
    makes ``"Unknown"`` distinguishable from a sentinel.
    """
    text = literal.strip()
    if len(text) < 6 or "\x01" in text:
        return None
    if _URL.search(text) or _DESCRIPTOR.match(text):
        return None
    if _RESOURCE_ID.match(text) or _DOTTED_ID.match(text) or _FILE_PATH.match(text):
        return None
    if " " not in text or _JVM_PATH.search(text):
        return None
    words = _WORD.findall(text)
    if len(words) < _MIN_ASSOCIATED_WORDS:
        return None
    # Mostly-symbol payloads: format templates, separators, ascii art.
    if sum(character.isalpha() or character.isspace() for character in text) / len(
        text
    ) < 0.8:
        return None
    if text in lang:
        return None
    return text


# --------------------------------------------------------------------------
# public API


@dataclass(frozen=True, slots=True)
class HardcodedString:
    """One display string found in compiled code rather than a lang file."""

    #: The English as the player sees it.
    text: str
    #: Declaring class, e.g. ``com/obscuria/aquamirae/…/AquamiraeCreativeTab``.
    class_name: str
    #: ``component`` (serialized text component) or ``associated``
    #: (multi-word label in a class that produced a component hit).
    kind: str


@dataclass(slots=True)
class HardcodedMod:
    """Per-mod finding: display text no language file can reach."""

    mod_id: str
    jar_name: str
    strings: list[HardcodedString] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.strings)


#: Hard ceiling on strings reported for one mod. A pathological jar
#: cannot turn the scan payload into megabytes; the count still tells the
#: user the real scale.
MAX_STRINGS_PER_MOD = 200


def find_hardcoded_strings(
    zf: zipfile.ZipFile,
    *,
    jar_name: str,
    mod_id: str,
    source_locale: str = "en_us",
) -> HardcodedMod | None:
    """Scan one already-open mod jar. ``None`` when it hardcodes nothing.

    Takes the caller's :class:`zipfile.ZipFile` so the jar is opened once
    per scan: the scanner is already inside the handle extracting lang
    files when this runs.
    """
    class_names = [
        name for name in zf.namelist() if name.endswith(".class") and not name.endswith("/")
    ]
    if not class_names:
        return None

    lang = LangIndex(source_lang_values(zf, source_locale))
    own = own_packages(class_names)

    # Pass 1: component hits, remembering each hit class's other literals
    # so the associated pass needs no second parse.
    found: list[HardcodedString] = []
    seen: set[str] = set()
    leftovers: dict[str, list[str]] = {}

    for name in class_names:
        if _is_foreign(name, own):
            continue
        try:
            parsed = parse_constant_pool(zf.read(name))
        except (zipfile.BadZipFile, OSError, KeyError, EOFError) as exc:
            logger.debug("Unreadable class entry %s in %s: %s", name, jar_name, exc)
            continue
        if parsed is None or not parsed.literals:
            continue
        if _is_lang_provider(name, parsed.refs):
            continue

        rest: list[str] = []
        hit = False
        for literal in parsed.literals:
            text = _component_hit(literal, lang)
            if text is None:
                rest.append(literal)
                continue
            hit = True
            if text not in seen:
                seen.add(text)
                found.append(
                    HardcodedString(text=text, class_name=name, kind="component")
                )
        if hit:
            leftovers[name] = rest

    if not found:
        return None

    # Pass 2: guilt by association, only inside classes proven above.
    for name, literals in leftovers.items():
        for literal in literals:
            text = _associated_hit(literal, lang)
            if text is None or text in seen:
                continue
            seen.add(text)
            found.append(
                HardcodedString(text=text, class_name=name, kind="associated")
            )

    found.sort(key=lambda s: (s.kind != "component", s.class_name, s.text))
    if len(found) > MAX_STRINGS_PER_MOD:
        logger.info(
            "Mod %s (%s) reported %d hardcoded strings; truncating to %d",
            mod_id,
            jar_name,
            len(found),
            MAX_STRINGS_PER_MOD,
        )
        found = found[:MAX_STRINGS_PER_MOD]

    logger.info(
        "Hardcoded display text in %s (%s): %d strings across %d classes",
        mod_id,
        jar_name,
        len(found),
        len({s.class_name for s in found}),
    )
    return HardcodedMod(mod_id=mod_id, jar_name=jar_name, strings=found)
