"""FTBQuests handler: embedded raw-JSON-text components are segment-split.

FTB packs embed Minecraft raw-JSON-text strings (tellraw-style arrays /
objects) inside quest descriptions. The handler must expose their
human-visible segments as ordinary entries while keeping structure,
styling, and click/hover events intact on write-back.
"""

from __future__ import annotations

import json
from pathlib import Path

import ftb_snbt_lib as slib
import pytest
from ftb_snbt_lib.tag import Compound, List, String

from moru_engine.handlers.ftbquests import FTBQuestsHandler
from moru_engine.parsers import BaseParser

CLICK_COMPONENT = (
    '["", {"text": "Click Here", "underlined": true, "color": "aqua", '
    '"clickEvent": {"action": "run_command", '
    '"value": "/open_guideme ftb:guide open"}}, '
    '" to open the guideme"]'
)
STYLED_OBJECT = '{"text": "So this is Helheim", "italic": true}'
NESTED_EXTRA = '{"text": "a", "extra": [{"text": "b"}, "c"]}'
NOT_A_COMPONENT = '{"value": 3}'


@pytest.fixture
def quest_path(tmp_path: Path) -> Path:
    return tmp_path / "config" / "ftbquests" / "quests" / "chapters" / "hel.snbt"


def _write_quest(path: Path, description: list[str]) -> None:
    """Author the fixture through ftb_snbt_lib so escaping matches reality."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = Compound(
        {
            "title": String("Chapter"),
            "quests": List(
                [
                    Compound(
                        {
                            "title": String("Quest"),
                            "description": List(
                                [String(line) for line in description]
                            ),
                        }
                    )
                ]
            ),
        }
    )
    path.write_text(slib.dumps(data), encoding="utf-8")


@pytest.mark.asyncio
async def test_extract_splits_component_segments(quest_path: Path) -> None:
    _write_quest(
        quest_path,
        [
            "plain line",
            CLICK_COMPONENT,
            "",
            STYLED_OBJECT,
            NESTED_EXTRA,
            NOT_A_COMPONENT,
        ],
    )
    entries = dict(await FTBQuestsHandler().extract(quest_path))

    assert entries["quests[0].description[0]"] == "plain line"
    # Array components: bare strings and object text fields in DFS order.
    assert entries["quests[0].description[1]::jsonseg[0]"] == "Click Here"
    assert (
        entries["quests[0].description[1]::jsonseg[1]"] == " to open the guideme"
    )
    assert (
        entries["quests[0].description[3]::jsonseg[0]"] == "So this is Helheim"
    )
    # extra arrays are visited after the owning component's text.
    assert entries["quests[0].description[4]::jsonseg[0]"] == "a"
    assert entries["quests[0].description[4]::jsonseg[1]"] == "b"
    assert entries["quests[0].description[4]::jsonseg[2]"] == "c"
    # The raw JSON never leaks as a whole entry, and JSON without text
    # components stays untranslatable.
    assert "quests[0].description[1]" not in entries
    assert not any(k.startswith("quests[0].description[5]") for k in entries)


@pytest.mark.asyncio
async def test_apply_rebuilds_components_with_structure_intact(
    quest_path: Path, tmp_path: Path
) -> None:
    _write_quest(quest_path, ["plain line", CLICK_COMPONENT, STYLED_OBJECT])

    out = tmp_path / "out" / "hel.snbt"
    await FTBQuestsHandler().apply(
        quest_path,
        {
            "quests[0].title": "퀘스트",
            "quests[0].description[0]": "일반 줄",
            "quests[0].description[1]::jsonseg[0]": "여기를 클릭",
            "quests[0].description[1]::jsonseg[1]": " 가이드를 여세요",
            "quests[0].description[2]::jsonseg[0]": "그래, 여기가 헬헤임이군",
        },
        out,
    )

    parser = BaseParser.create_parser(out, original_path=quest_path)
    assert parser is not None
    flat = dict(await parser.parse())
    assert flat["quests[0].title"] == "퀘스트"
    assert flat["quests[0].description[0]"] == "일반 줄"

    component = json.loads(flat["quests[0].description[1]"])
    assert component[0] == ""  # empty padding segment untouched
    assert component[1]["text"] == "여기를 클릭"
    assert component[1]["color"] == "aqua"
    assert component[1]["underlined"] is True
    # Event payloads survive verbatim - commands must never be translated.
    assert component[1]["clickEvent"] == {
        "action": "run_command",
        "value": "/open_guideme ftb:guide open",
    }
    assert component[2] == " 가이드를 여세요"

    styled = json.loads(flat["quests[0].description[2]"])
    assert styled == {"text": "그래, 여기가 헬헤임이군", "italic": True}


@pytest.mark.asyncio
async def test_apply_without_segment_translations_keeps_component(
    quest_path: Path, tmp_path: Path
) -> None:
    _write_quest(quest_path, [CLICK_COMPONENT])

    out = tmp_path / "out" / "hel.snbt"
    await FTBQuestsHandler().apply(quest_path, {"quests[0].title": "퀘스트"}, out)

    parser = BaseParser.create_parser(out, original_path=quest_path)
    assert parser is not None
    flat = dict(await parser.parse())
    assert json.loads(flat["quests[0].description[0]"]) == json.loads(
        CLICK_COMPONENT
    )
