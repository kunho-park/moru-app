"""Heracles content handler for extracting translatable quest text."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from ..parsers import BaseParser, DumpError, ParseError
from .base import ContentHandler, is_translation_key_reference

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

#: ``display.title``/``display.subtitle`` are full Minecraft Components
#: (``ExtraCodecs.COMPONENT`` on 1.20.x, ``ComponentSerialization.CODEC``
#: on 1.21.1), so the same slot appears on disk as a bare string, as
#: ``{"text": ...}``, or as ``{"translate": ...}``, optionally with
#: ``extra`` children — real packs use all three for the same field.
#:
#: ``translate`` is included ON PURPOSE. The in-game editor writes
#: ``Component.translatable(userInput)``, so a hand-authored title lands
#: in ``translate`` as PROSE ("Gantry", "The Eye Map") and Minecraft
#: renders the key verbatim when no translation exists. Rejecting
#: ``translate`` wholesale would skip nearly every quest title; telling
#: prose from a real lang key is what ``is_translation_key_reference``
#: does. Style fields and ``with`` substitution arguments are excluded.
_DISPLAY_TEXT_RE = re.compile(
    r"^display\.(?:title|subtitle)(?:\.extra\[\d+\])*(?:\.(?:text|translate))?$"
)

#: ``description`` is ``Codec.either(STRING, STRING.listOf())``, so it is
#: one string or an array of them — plain strings carrying Heracles' own
#: markdown/Hermes markup, never Component JSON.
_DESCRIPTION_RE = re.compile(r"^display\.description(?:\[\d+\])?$")

#: Task and reward ``title``/``description``. Both nest: ``heracles:
#: composite`` carries child ``tasks`` and ``heracles:selectable`` child
#: ``rewards``, arbitrarily deep. A non-empty task/reward ``title`` is
#: rendered through ``CustomizableQuestElement.titleOr``, which wraps it
#: in ``Component.translatable`` — same prose-or-key duality as above.
#: ``heracles:location`` is the one task whose ``description`` is a
#: Component rather than a plain string, hence the optional text step.
_ELEMENT_TEXT_RE = re.compile(
    r"^(?:tasks|rewards)\.[^.]+(?:\.(?:tasks|rewards)\.[^.]+)*"
    r"\.(?:title|description)(?:(?:\.extra\[\d+\])*\.(?:text|translate))?$"
)

#: Item NBT holds prose-looking display names ("Create Common Lootbag")
#: that are item IDENTITY used for stack matching, not text Heracles
#: owns. Never descend into these, whatever the leaf is called.
_OPAQUE_SEGMENTS = frozenset({"nbt", "tag"})

#: A description segment whose payload is a lang key resolved at render
#: time. Its text lives in a language file, which is translated there.
_MARKUP_TRANSLATE = "<translate>"


class HeraclesHandler(ContentHandler):
    """Handler for Heracles quest JSON files.

    Extracted: ``display.title``/``display.subtitle`` in any of their
    Component spellings, ``display.description`` segments, and task and
    reward ``title``/``description``.

    Not extracted, deliberately: ``display.groups`` keys, which are the
    group label AND the join key to ``groups.txt`` AND the source of the
    on-disk folder name, so renaming one moves files and breaks lookups;
    anything under an ``nbt``/``tag`` object; ``icon``/``icon_background``
    and every ``settings`` enum; a reward's ``command``; and description
    segments carrying ``<translate>``, whose payload is a lang key.

    Markup inside descriptions is not parsed here. Hermes tags
    (``<text>``, ``<img src=...>``, ``<hr/>``) are frozen by the shared
    placeholder protection before the text reaches a model, and a segment
    that is nothing but markup becomes placeholder-only and is skipped
    upstream, so no handler-side markup pass is needed.
    """

    name: ClassVar[str] = "heracles"
    priority: ClassVar[int] = 10

    path_patterns: ClassVar[tuple[str, ...]] = (
        "/heracles/quests/",
        "\\heracles\\quests\\",
    )

    extensions: ClassVar[tuple[str, ...]] = (".json",)

    def can_handle(self, path: Path) -> bool:
        """Check if this is a Heracles quest file."""
        if path.suffix.lower() not in self.extensions:
            return False

        path_str = str(path).replace("\\", "/").lower()
        return any(
            p.lower().replace("\\", "/") in path_str for p in self.path_patterns
        )

    @staticmethod
    def _is_text_field(key: str) -> bool:
        """Whether a flattened key addresses quest prose."""
        if any(
            segment.split("[")[0] in _OPAQUE_SEGMENTS for segment in key.split(".")
        ):
            return False
        return bool(
            _DISPLAY_TEXT_RE.match(key)
            or _DESCRIPTION_RE.match(key)
            or _ELEMENT_TEXT_RE.match(key)
        )

    async def extract(self, path: Path) -> Mapping[str, str]:
        """Extract translatable strings from a Heracles quest file."""
        parser = BaseParser.create_parser(path)
        if parser is None:
            logger.warning("No parser found for: %s", path)
            return {}

        try:
            raw_data = await parser.parse()
        except (ParseError, OSError) as e:
            logger.error("Failed to parse %s: %s", path, e)
            return {}

        entries = {
            key: value
            for key, value in raw_data.items()
            if self._is_text_field(key)
            and value.strip()
            and _MARKUP_TRANSLATE not in value
            and not is_translation_key_reference(value)
        }

        logger.debug(
            "Extracted %d entries from Heracles file: %s", len(entries), path.name
        )
        return entries

    async def apply(
        self,
        path: Path,
        translations: Mapping[str, str],
        output_path: Path | None = None,
    ) -> None:
        """Apply translations to a Heracles quest file."""
        target_path = output_path or path

        target_path.parent.mkdir(parents=True, exist_ok=True)

        output_parser = BaseParser.create_parser(target_path, original_path=path)
        if output_parser is None:
            logger.warning("No parser found for output: %s", target_path)
            return

        try:
            await output_parser.dump(translations)
            logger.debug("Applied translations to: %s", target_path.name)
        except (DumpError, OSError) as e:
            logger.error("Failed to write %s: %s", target_path, e)
            raise
