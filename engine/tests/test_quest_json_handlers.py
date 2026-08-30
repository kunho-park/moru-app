"""BetterQuesting and Heracles: whitelist prose, never IDs or lang keys.

Both formats bury a little author-written prose in a lot of structural
data, and in both the same TAG_String / plain-string slot may hold either
the text or a lang key the game resolves at runtime. Translating a key
breaks the lookup, and translating an id breaks the quest, so the tests
below pin both directions on shapes taken from real pack files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moru_engine.handlers import BetterQuestingHandler, HeraclesHandler
from moru_engine.handlers.base import (
    create_default_registry,
    is_translation_key_reference,
)

# --- shapes copied from real files -----------------------------------------

#: config/betterquesting/DefaultQuests.json (Enigmatica2Expert, format 2.0.0).
#: The ":8"/":3"/":9"/":10" suffixes are NBT tag ids fused onto the key, and
#: NBT lists are objects keyed by stringified index.
BQ_MONOLITHIC = {
    "format:8": "2.0.0",
    "questDatabase:9": {
        "0:10": {
            "questID:3": 0,
            "properties:10": {
                "betterquesting:10": {
                    "name:8": "Creative RF",
                    "desc:8": "Infinite RF",
                    "icon:10": {"id:8": "enderio:block_cap_bank", "OreDict:8": ""},
                    "snd_complete:8": "minecraft:entity.player.levelup",
                    "questlogic:8": "AND",
                    "visibility:8": "ALWAYS",
                    "partySingleReward:8": "false",
                    "isMain:8": "false",
                }
            },
            "tasks:9": {
                "0:10": {
                    "taskID:8": "bq_standard:retrieval",
                    "ignoreNBT:1": 0,
                    "requiredItems:9": {
                        "0:10": {
                            "id:8": "gregtech:gt.metaitem.01",
                            "OreDict:8": "itemFlint",
                            "tag:10": {"display:10": {"Name:8": "Iron Bolts"}},
                        }
                    },
                }
            },
            "rewards:9": {
                "0:10": {
                    "rewardID:8": "bq_standard:command",
                    "title:8": "bq_standard.reward.command",
                    "description:8": "Run a command script",
                    "command:8": "/say VAR_NAME has obtained the Capacitor Bank!",
                }
            },
        }
    },
    "questLines:9": {
        "0:10": {
            "lineID:3": 0,
            "properties:10": {
                "betterquesting:10": {
                    "name:8": "Getting Started",
                    "desc:8": "This chapter explains how to get started.",
                    "bg_image:8": "",
                }
            },
            "quests:9": {"0:10": {"id:3": 0, "x:3": 132}},
        }
    },
    #: pack_name looks like a title but is the pack-update identity key, and
    #: it nests betterquesting directly with no properties step.
    "questSettings:10": {
        "betterquesting:10": {
            "pack_name:8": "Enigmatica 2: Expert",
            "home_image:8": "modpack:textures/betterquesting/goal.png",
        }
    },
}

#: config/betterquesting/DefaultQuests/Quests/**.json (GTNewHorizons fork):
#: the file IS one quest, and the fork adds notification strings.
BQ_SPLIT_QUEST = {
    "questIDHigh:4": 0,
    "questIDLow:4": 3039,
    "properties:10": {
        "betterquesting:10": {
            "name:8": "§9§l§nDyson Swarm Ground Unit",
            "desc:8": "The multiblock is basically a solar panel.\n- Needs coolant.",
            "notification_title:8": "Swarm online",
            "notification_subtitle:8": "Ground unit built",
            "notification_style:8": "default",
            "completion_animation:8": "default",
            "taskLogic:8": "AND",
            "snd_update:8": "random.levelup",
        }
    },
    "tasks:9": {},
    "rewards:9": {},
}

#: config/betterquesting/DefaultQuests/QuestLines/1.json (Supersymmetry):
#: this pack localises quests through lang files, so the SAME slots hold
#: keys, not prose.
BQ_LANG_KEY_LINE = {
    "lineID:3": 1,
    "properties:10": {
        "betterquesting:10": {
            "desc:8": "susy.quest.ql.1.desc",
            "name:8": "susy.quest.ql.1.title",
            "icon:10": {"id:8": "gregtech:meta_item_1"},
        }
    },
}

#: config/heracles/quests/**.json. title/subtitle are Components, so the
#: same slot appears as a bare string, {"text":...} or {"translate":...} —
#: and the in-game editor writes PROSE into translate.
HERACLES_QUEST = {
    "dependencies": ["root"],
    "tasks": {
        "six_eyes": {
            "title": "Collect 6 or more of the 12 custom eyes",
            "type": "heracles:check",
            "icon": {"item": {"id": "minecraft:ender_eye", "count": 1}},
        },
        "any_crop": {
            "type": "heracles:composite",
            "tasks": {
                "carrot": {
                    "title": "Grow a carrot",
                    "type": "heracles:item",
                    "item": "minecraft:carrot",
                    "collection": "AUTOMATIC",
                }
            },
        },
    },
    "rewards": {
        "shout": {
            "type": "heracles:command",
            "title": "Announce it",
            "command": "/say VAR_NAME finished the hunt",
        },
        "bow": {
            "type": "heracles:item",
            "item": {
                "id": "minecraft:bow",
                "nbt": {"display": {"Name": "Create Common Lootbag"}},
            },
        },
    },
    "display": {
        "title": {"translate": "The Eye Map"},
        "subtitle": {"text": "Nine civilizations. Three anchors."},
        "description": [
            "<h2>Used to extract sap from rubber trees.</h2>",
            "The Sap is placed into the inventory of the player using it.",
        ],
        "groups": {"Main": {"position": [-230, -200]}},
        "icon": {"item": {"id": "minecraft:composter", "count": 1}},
        "icon_background": "heracles:textures/gui/quest_backgrounds/default.png",
    },
    "settings": {"hidden": "LOCKED", "repeatable": False},
}

#: The CaveStone shape: everything routed through lang files.
HERACLES_LANG_KEY_QUEST = {
    "display": {
        "title": {"translate": "cavestone.quests.main.composter"},
        "subtitle": {"translate": "cavestone.quests.main.composter.desc"},
        "description": [
            "<text><translate>cavestone.quests.main.composter.explanation</translate></text>"
        ],
    }
}


def _write(path: Path, data: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# --- routing ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("config/betterquesting/DefaultQuests.json", "betterquesting"),
        ("DefaultQuests.json", "betterquesting"),
        (
            "config/betterquesting/DefaultQuests/Quests/Line/Dyson.json",
            "betterquesting",
        ),
        ("config/betterquesting/DefaultQuests/QuestLines/1.json", "betterquesting"),
        ("config/heracles/quests/main/root.json", "heracles"),
        ("data/common/config/heracles/quests/create/lava.json", "heracles"),
    ],
)
def test_registry_routes_real_layouts(tmp_path: Path, rel: str, expected: str) -> None:
    path = _write(tmp_path / rel, {})
    handler = create_default_registry().get_handler(path)

    assert handler is not None
    assert handler.name == expected


def test_unrelated_json_is_not_claimed(tmp_path: Path) -> None:
    path = _write(tmp_path / "config" / "betterquesting" / "questbook.txt", {})
    assert BetterQuestingHandler().can_handle(path) is False
    assert HeraclesHandler().can_handle(tmp_path / "config" / "other" / "x.json") is False


# --- BetterQuesting extraction --------------------------------------------


async def test_monolithic_database_yields_only_quest_prose(tmp_path: Path) -> None:
    path = _write(tmp_path / "config/betterquesting/DefaultQuests.json", BQ_MONOLITHIC)

    entries = await BetterQuestingHandler().extract(path)

    assert entries == {
        "questDatabase:9.0:10.properties:10.betterquesting:10.name:8": "Creative RF",
        "questDatabase:9.0:10.properties:10.betterquesting:10.desc:8": "Infinite RF",
        "questLines:9.0:10.properties:10.betterquesting:10.name:8": "Getting Started",
        "questLines:9.0:10.properties:10.betterquesting:10.desc:8": (
            "This chapter explains how to get started."
        ),
    }


@pytest.mark.parametrize(
    "excluded",
    [
        "Iron Bolts",  # item NBT display name: COMPARED when ignoreNBT is 0
        "/say VAR_NAME has obtained the Capacitor Bank!",  # executed
        "Run a command script",  # mod-supplied reward default
        "Enigmatica 2: Expert",  # pack-update identity key
        "bq_standard:retrieval",  # registry ids
        "bq_standard:command",
        "enderio:block_cap_bank",  # item id
        "itemFlint",  # ore dictionary name
        "minecraft:entity.player.levelup",  # sound event
        "AND",  # logic enum
        "ALWAYS",  # visibility enum
        "false",  # boolean stored as TAG_String
        "modpack:textures/betterquesting/goal.png",  # resource path
        "2.0.0",  # format marker
    ],
)
async def test_confusable_strings_are_never_extracted(
    tmp_path: Path, excluded: str
) -> None:
    path = _write(tmp_path / "config/betterquesting/DefaultQuests.json", BQ_MONOLITHIC)

    values = set((await BetterQuestingHandler().extract(path)).values())

    assert excluded not in values


async def test_split_layout_includes_fork_notification_strings(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "config/betterquesting/DefaultQuests/Quests/L/Dyson.json",
        BQ_SPLIT_QUEST,
    )

    entries = await BetterQuestingHandler().extract(path)

    prefix = "properties:10.betterquesting:10."
    assert entries == {
        f"{prefix}name:8": "§9§l§nDyson Swarm Ground Unit",
        f"{prefix}desc:8": (
            "The multiblock is basically a solar panel.\n- Needs coolant."
        ),
        f"{prefix}notification_title:8": "Swarm online",
        f"{prefix}notification_subtitle:8": "Ground unit built",
    }


async def test_lang_key_valued_quest_yields_nothing(tmp_path: Path) -> None:
    # Writing Korean over "susy.quest.ql.1.title" would break the lookup and
    # render the raw key; the lang file it points at is translated on its own.
    path = _write(
        tmp_path / "config/betterquesting/DefaultQuests/QuestLines/1.json",
        BQ_LANG_KEY_LINE,
    )

    assert await BetterQuestingHandler().extract(path) == {}


# --- Heracles extraction --------------------------------------------------


async def test_heracles_extracts_prose_from_every_component_spelling(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path / "config/heracles/quests/main/eye.json", HERACLES_QUEST)

    entries = await HeraclesHandler().extract(path)

    assert entries == {
        # translate carries PROSE here: the editor wraps user input in
        # Component.translatable, and MC renders the key verbatim.
        "display.title.translate": "The Eye Map",
        "display.subtitle.text": "Nine civilizations. Three anchors.",
        "display.description[0]": "<h2>Used to extract sap from rubber trees.</h2>",
        "display.description[1]": (
            "The Sap is placed into the inventory of the player using it."
        ),
        "tasks.six_eyes.title": "Collect 6 or more of the 12 custom eyes",
        "tasks.any_crop.tasks.carrot.title": "Grow a carrot",
        "rewards.shout.title": "Announce it",
    }


@pytest.mark.parametrize(
    "excluded",
    [
        "Create Common Lootbag",  # item NBT identity, used for matching
        "/say VAR_NAME finished the hunt",  # executed
        "Main",  # group key: label AND join key AND folder name
        "heracles:textures/gui/quest_backgrounds/default.png",
        "minecraft:composter",
        "minecraft:carrot",
        "heracles:check",
        "AUTOMATIC",
        "LOCKED",
        "root",  # dependency id
    ],
)
async def test_heracles_never_extracts_ids_or_nbt(
    tmp_path: Path, excluded: str
) -> None:
    path = _write(tmp_path / "config/heracles/quests/main/eye.json", HERACLES_QUEST)

    values = set((await HeraclesHandler().extract(path)).values())

    assert excluded not in values


async def test_heracles_lang_key_quest_yields_nothing(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config/heracles/quests/main/composter.json",
        HERACLES_LANG_KEY_QUEST,
    )

    # translate holding a real key, and a description whose payload is a
    # <translate> markup reference, are both skipped.
    assert await HeraclesHandler().extract(path) == {}


# --- round-trip -----------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "rel", "data"),
    [
        (
            BetterQuestingHandler(),
            "config/betterquesting/DefaultQuests.json",
            BQ_MONOLITHIC,
        ),
        (
            BetterQuestingHandler(),
            "config/betterquesting/DefaultQuests/Quests/L/D.json",
            BQ_SPLIT_QUEST,
        ),
        (HeraclesHandler(), "config/heracles/quests/main/eye.json", HERACLES_QUEST),
    ],
)
async def test_apply_touches_only_the_extracted_leaves(
    tmp_path: Path,
    handler: BetterQuestingHandler | HeraclesHandler,
    rel: str,
    data: dict[str, object],
) -> None:
    path = _write(tmp_path / rel, data)
    entries = dict(await handler.extract(path))
    out = tmp_path / "out" / rel

    await handler.apply(path, {k: f"[ko]{v}" for k, v in entries.items()}, out)

    original = json.loads(path.read_text(encoding="utf-8"))
    after = json.loads(out.read_text(encoding="utf-8"))
    assert _changed_keys(original, after) == set(entries)


async def test_identity_apply_reproduces_the_source_data(tmp_path: Path) -> None:
    path = _write(tmp_path / "config/betterquesting/DefaultQuests.json", BQ_MONOLITHIC)
    handler = BetterQuestingHandler()
    entries = dict(await handler.extract(path))
    out = tmp_path / "out" / "DefaultQuests.json"

    await handler.apply(path, entries, out)

    assert json.loads(out.read_text(encoding="utf-8")) == BQ_MONOLITHIC


def _changed_keys(a: object, b: object, prefix: str = "") -> set[str]:
    """Flattened keys whose scalar differs; asserts shape is untouched."""
    changed: set[str] = set()
    if isinstance(a, dict) and isinstance(b, dict):
        assert list(a) == list(b), f"key set or order changed at {prefix!r}"
        for key in a:
            child = f"{prefix}.{key}" if prefix else key
            changed |= _changed_keys(a[key], b[key], child)
    elif isinstance(a, list) and isinstance(b, list):
        assert len(a) == len(b), f"list length changed at {prefix!r}"
        for index, (x, y) in enumerate(zip(a, b, strict=True)):
            changed |= _changed_keys(x, y, f"{prefix}[{index}]")
    elif a != b:
        changed.add(prefix)
    return changed


# --- the shared lang-key predicate ---------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "susy.quest.ql.1.desc",  # numeric segment: the case that motivated it
        "cavestone.quests.main.composter",
        "patchouli.mod.book.entry.name",
        "untitled.name",  # BetterQuesting's own default property
        "bq_standard.reward.command",
    ],
)
def test_lang_keys_are_recognised(value: str) -> None:
    assert is_translation_key_reference(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "1.20.1",  # dotted version strings are real values in quest text
        "1.0",
        "2.7.5",
        "v1.20.1",  # one lettered segment is not a namespace plus a path
        "alpha.2.0",
        "Getting Started",
        "The Eye Map",
        "Gantry",
        "Infinite RF",
        "e.g.",
        "§9§l§nDyson Swarm Ground Unit",
        "minecraft:book",
    ],
)
def test_prose_and_versions_are_not_mistaken_for_keys(value: str) -> None:
    assert is_translation_key_reference(value) is False
