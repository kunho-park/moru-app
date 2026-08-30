"""BetterQuesting content handler for extracting translatable quest text."""

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

#: BetterQuesting writes NBT as JSON with the tag id fused onto the KEY:
#: ``name:8`` is TAG_String, ``sizeX:3`` int, ``questIDLow:4`` long,
#: ``autoClaim:1`` byte, ``tasks:9`` list, ``properties:10`` compound,
#: ``preRequisites:11`` int array. NBT lists become objects keyed by the
#: stringified index (``"0:10"``), never JSON arrays — which is why the
#: container step below is ``[^.]+`` and not ``\[\d+\]``.
_PROPERTIES = r"properties:10\.betterquesting:10\."

#: The only properties holding author-written prose. Everything else in
#: that compound is an id, an enum, a sound, a resource path, or — in some
#: builds — a boolean stored as a string ("partySingleReward:8": "false").
#: ``notification_title``/``notification_subtitle`` are the GTNH fork's
#: toast strings. The 2.0.0 writer lowercases several sibling keys
#: (``tasklogic``, ``ismain``) while 3.x camelCases them; these four are
#: spelled identically in every build, which is one more reason to
#: whitelist exactly them.
_TEXT_FIELDS = r"(?:name|desc|notification_title|notification_subtitle):8"

#: Split layout: the file IS one quest or one quest line.
_FIELD_RE = re.compile(rf"^{_PROPERTIES}{_TEXT_FIELDS}$")

#: Monolithic layout: quests and lines sit in index-keyed containers.
#: ``questSettings`` is deliberately unreachable from here — it nests
#: ``betterquesting:10`` directly with no ``properties:10`` step, and its
#: ``pack_name:8`` looks like a title but is the pack-update identity key.
_DATABASE_FIELD_RE = re.compile(
    rf"^(?:questDatabase|questLines):9\.[^.]+\.{_PROPERTIES}{_TEXT_FIELDS}$"
)


class BetterQuestingHandler(ContentHandler):
    """Handler for BetterQuesting quest databases.

    Two on-disk layouts share one inner shape and both are supported:
    the classic single ``config/betterquesting/DefaultQuests.json``, and
    the split ``config/betterquesting/DefaultQuests/{Quests,QuestLines}``
    tree where each file is one quest or line.

    Extracted: quest and quest-line ``name``/``desc``, plus the fork's
    ``notification_title``/``notification_subtitle``.

    Everything else is left alone on purpose. Item ids, ore-dictionary
    names, ``taskID``/``rewardID``, logic enums, sound events, background
    images and string-encoded booleans all arrive as TAG_String and all
    look translatable. Two are actively dangerous: a reward's ``command``
    is executed, and item NBT display names inside a task are COMPARED
    when that task does not ignore NBT, so translating one makes the quest
    permanently uncompletable.
    """

    name: ClassVar[str] = "betterquesting"
    priority: ClassVar[int] = 10

    path_patterns: ClassVar[tuple[str, ...]] = (
        "/betterquesting/",
        "\\betterquesting\\",
    )

    extensions: ClassVar[tuple[str, ...]] = (".json",)

    #: Packs also ship the database standalone for import.
    STANDALONE_NAME: ClassVar[str] = "defaultquests.json"

    def can_handle(self, path: Path) -> bool:
        """Check if this is a BetterQuesting quest file."""
        if path.suffix.lower() not in self.extensions:
            return False

        path_str = str(path).replace("\\", "/").lower()
        if path.name.lower() == self.STANDALONE_NAME:
            return True
        return any(
            p.lower().replace("\\", "/") in path_str for p in self.path_patterns
        )

    @staticmethod
    def _is_text_field(key: str) -> bool:
        """Whether a flattened key addresses quest prose."""
        return bool(_FIELD_RE.match(key) or _DATABASE_FIELD_RE.match(key))

    async def extract(self, path: Path) -> Mapping[str, str]:
        """Extract translatable strings from a BetterQuesting file."""
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
            # A pack may store a lang key here instead of the prose;
            # BetterQuesting resolves it when the key exists and renders
            # the raw string otherwise. Its own default properties
            # ("untitled.name") are keys too.
            and not is_translation_key_reference(value)
        }

        logger.debug(
            "Extracted %d entries from BetterQuesting file: %s", len(entries), path.name
        )
        return entries

    async def apply(
        self,
        path: Path,
        translations: Mapping[str, str],
        output_path: Path | None = None,
    ) -> None:
        """Apply translations to a BetterQuesting file."""
        target_path = output_path or path

        target_path.parent.mkdir(parents=True, exist_ok=True)

        output_parser = BaseParser.create_parser(target_path, original_path=path)
        if output_parser is None:
            logger.warning("No parser found for output: %s", target_path)
            return

        try:
            # dump() rebuilds from the ORIGINAL structure and overwrites
            # only the flattened paths handed to it, so every untouched
            # tag, index-keyed container and type suffix survives.
            await output_parser.dump(translations)
            logger.debug("Applied translations to: %s", target_path.name)
        except (DumpError, OSError) as e:
            logger.error("Failed to write %s: %s", target_path, e)
            raise
