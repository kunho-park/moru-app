"""Modtext harvest + builder integration: filters, splits, determinism."""

from __future__ import annotations

import json
import re
import zipfile
from collections import Counter
from pathlib import Path

import pytest

from moru_engine.evalset import build_evalset, harvest_pack, load_goldset
from moru_engine.evalset.builder import (
    _modtext_sibling_block,
    _modtext_stem,
    build_modtext_examples,
)
from moru_engine.evalset.modtext import MODTEXT_FORMAT
from moru_engine.graph import SIBLING_CONTEXT_HEADER

PH_RE = re.compile(r"\{\{[A-Z]+\d*\}\}")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _make_pack(root: Path) -> Path:
    """Synthetic pack exercising every category and every drop reason."""
    pack = root / "pack"
    _write_json(pack / "manifest.json", {"name": "Test Pack", "version": "1.0"})
    assets = pack / "overrides" / "kubejs" / "assets"

    _write_json(
        assets / "farm" / "lang" / "en_us.json",
        {
            "item.farm.hoe": "Iron Hoe",
            "tooltip.farm.keg": "&6Wine Keg&r ferments %s juices",
            "msg.farm.copy": "Untranslated",
            "msg.farm.latin": "No hangul here",
            "msg.farm.ratio": "Hi",
            "msg.farm.badtoken": "&6Gold&r nugget",
            "msg.farm.dupe": "Iron Hoe",
            "msg.farm.only_ph": "%s",
        },
    )
    _write_json(
        assets / "farm" / "lang" / "ko_kr.json",
        {
            "item.farm.hoe": "철 괭이",
            "tooltip.farm.keg": "&6와인통&r은 %s 주스를 발효시킵니다",
            "msg.farm.copy": "Untranslated",
            "msg.farm.latin": "Pas de coreen",
            "msg.farm.ratio": "지나치게 길어진 번역문이라 길이 비율 검사를 통과하지 못합니다",
            "msg.farm.badtoken": "금 조각",
            "msg.farm.dupe": "철 괭이",
            "msg.farm.only_ph": "%s",
        },
    )
    # vanilla override: must be excluded wholesale (split-hygiene with
    # the vanilla strata key namespace)
    _write_json(
        assets / "minecraft" / "lang" / "en_us.json",
        {"block.minecraft.stone": "Stone"},
    )
    _write_json(
        assets / "minecraft" / "lang" / "ko_kr.json",
        {"block.minecraft.stone": "돌"},
    )
    # categorized namespaces
    _write_json(
        assets / "ftbquestlocalizer" / "lang" / "en_us.json",
        {"quest.q1.title": "A very long harvest quest"},
    )
    _write_json(
        assets / "ftbquestlocalizer" / "lang" / "ko_kr.json",
        {"quest.q1.title": "아주 긴 수확 퀘스트"},
    )
    _write_json(
        assets / "dialog" / "lang" / "en_us.json",
        {"npc.greet": "Nice weather today, friend!"},
    )
    _write_json(
        assets / "dialog" / "lang" / "ko_kr.json",
        {"npc.greet": "오늘 날씨 참 좋네, 친구!"},
    )
    # patchouli parallel book pages
    book = pack / "overrides" / "patchouli_books" / "almanac"
    _write_json(
        book / "en_us" / "entries" / "apple.json",
        {
            "name": "Apple Tree",
            "pages": [{"title": "Care", "text": "Water the sapling daily."}],
        },
    )
    _write_json(
        book / "ko_kr" / "entries" / "apple.json",
        {
            "name": "사과나무",
            "pages": [{"title": "관리", "text": "묘목에 매일 물을 주세요."}],
        },
    )
    return pack


def test_harvest_filters_and_categories(tmp_path: Path) -> None:
    goldset = harvest_pack(_make_pack(tmp_path))
    assert goldset["format"] == MODTEXT_FORMAT
    assert goldset["pack"] == "Test Pack 1.0"

    pairs = {p["key"]: p for p in goldset["pairs"]}
    assert set(pairs) == {
        "farm:item.farm.hoe",
        "farm:tooltip.farm.keg",
        "ftbquestlocalizer:quest.q1.title",
        "dialog:npc.greet",
        "patchouli:almanac/entries/apple#name",
        "patchouli:almanac/entries/apple#title0",
        "patchouli:almanac/entries/apple#text0",
    }
    assert pairs["ftbquestlocalizer:quest.q1.title"]["category"] == "quest"
    assert pairs["dialog:npc.greet"]["category"] == "dialog"
    assert pairs["farm:item.farm.hoe"]["category"] == "lang"
    assert pairs["patchouli:almanac/entries/apple#text0"]["category"] == "patchouli"

    dropped = goldset["stats"]["dropped"]
    assert dropped["untranslated_copy"] == 1
    assert dropped["source_only_placeholders"] == 1
    assert dropped["missing_target_script"] == 1
    assert dropped["length_ratio"] == 1
    assert dropped["format_literal_mismatch"] == 1
    assert dropped["duplicate_pair"] == 1
    # no minecraft:* key ever survives
    assert not any(k.startswith("minecraft:") for k in pairs)


def test_harvest_zip_matches_directory(tmp_path: Path) -> None:
    pack = _make_pack(tmp_path)
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for file in sorted(pack.rglob("*.json")):
            zf.write(file, file.relative_to(pack).as_posix())
    assert harvest_pack(zip_path) == harvest_pack(pack)


def test_goldset_roundtrip_and_validation(tmp_path: Path) -> None:
    goldset = harvest_pack(_make_pack(tmp_path))
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(goldset, ensure_ascii=False), encoding="utf-8")
    assert load_goldset(path)["pairs"] == goldset["pairs"]

    path.write_text(json.dumps({"format": "other"}), encoding="utf-8")
    with pytest.raises(ValueError, match="expected format"):
        load_goldset(path)


def _synthetic_goldset(n: int = 60) -> dict[str, object]:
    categories = ("quest", "dialog", "lang", "patchouli")
    return {
        "format": MODTEXT_FORMAT,
        "pack": "Synth Pack",
        "source_lang": "en_us",
        "target_lang": "ko_kr",
        "pairs": [
            {
                "key": f"synth:entry{i}",
                "category": categories[i % len(categories)],
                "source": f"&6Quest {i}&r needs %s items",
                "target": f"&6퀘스트 {i}&r에는 %s개가 필요합니다",
            }
            for i in range(n)
        ],
    }


def _sibling_goldset() -> dict[str, object]:
    """12 quest title/description groups, 2 patchouli entries, 8 loners."""
    pairs: list[dict[str, str]] = []
    for i in range(12):
        for field in ("title", "description1", "description2"):
            pairs.append(
                {
                    "key": f"synth:chapter.q{i}.{field}",
                    "category": "quest",
                    "source": f"Quest {i} {field} text",
                    "target": f"퀘스트 {i} {field} 텍스트",
                }
            )
    for book_entry in ("apple", "berry"):
        for field in ("name", "title0", "text0"):
            pairs.append(
                {
                    "key": f"patchouli:almanac/entries/{book_entry}#{field}",
                    "category": "patchouli",
                    "source": f"{book_entry} {field} text",
                    "target": f"{book_entry} {field} 텍스트",
                }
            )
    pairs += [
        {
            "key": f"synth:msg.standalone{i}",
            "category": "lang",
            "source": f"Standalone {i}",
            "target": f"단독 {i}",
        }
        for i in range(8)
    ]
    return {
        "format": MODTEXT_FORMAT,
        "pack": "Synth Pack",
        "source_lang": "en_us",
        "target_lang": "ko_kr",
        "pairs": pairs,
    }


def test_modtext_stem_grouping_rules() -> None:
    assert _modtext_stem("synth:chapter.q1.title") == "synth:chapter.q1"
    assert _modtext_stem("synth:chapter.q1.description1") == "synth:chapter.q1"
    assert _modtext_stem("synth:q.description[3]") == "synth:q"
    assert (
        _modtext_stem("patchouli:almanac/entries/apple#text0")
        == "patchouli:almanac/entries/apple"
    )
    assert _modtext_stem("synth:item.mod.hoe") is None  # not a sibling field
    assert _modtext_stem("standalone") is None  # no dot structure


def test_sibling_block_format_exclusion_and_caps() -> None:
    keys = [f"ns:obj.description{i}" for i in range(12)] + ["ns:obj.title"]
    pairs = {
        k: {"source": f"src {k.rpartition('.')[2]}", "target": f"골드 {k.rpartition('.')[2]}"}
        for k in keys
    }
    group = sorted(keys)
    group_of = {k: group for k in keys}

    block = _modtext_sibling_block(["ns:obj.description0"], group_of, pairs)
    lines = block.splitlines()
    # own batch excluded, cap at 8 lines, production line shape
    assert len(lines) == 8
    assert all(line.startswith("- obj.") for line in lines)
    assert "ns:" not in block
    assert "description0" not in block
    assert '- obj.description1: "src description1" => "골드 description1"' in lines

    # whole group inside the batch -> nothing to quote
    assert _modtext_sibling_block(group, group_of, pairs) == ""


def test_modtext_sibling_context_is_split_local_and_leak_free() -> None:
    goldset = _sibling_goldset()
    n = len(goldset["pairs"])
    split = build_modtext_examples(goldset, samples=n, batch_size=2, seed=42)

    key_split = {
        key: name
        for name, examples in split.items()
        for ex in examples
        for key in ex.entries
    }
    # sibling groups are split-atomic: one quest never straddles buckets
    group_splits: dict[str, set[str]] = {}
    for key, name in key_split.items():
        stem = _modtext_stem(key)
        if stem is not None:
            group_splits.setdefault(stem, set()).add(name)
    assert group_splits and all(len(s) == 1 for s in group_splits.values())
    quote_re = re.compile(r"^- (.+?): \"", re.MULTILINE)
    headers = 0
    for name, examples in split.items():
        for ex in examples:
            if SIBLING_CONTEXT_HEADER not in ex.context:
                continue
            headers += 1
            block = ex.context.split(SIBLING_CONTEXT_HEADER, 1)[1]
            quoted = quote_re.findall(block)
            assert quoted, ex.context
            for display in quoted:
                full = (
                    f"patchouli:{display}"
                    if display.startswith("almanac/")
                    else f"synth:{display}"
                )
                # quoted gold comes from THIS split, never another one
                assert key_split[full] == name, (full, name)
                # and never quotes an entry of the batch itself
                assert full not in ex.entries
    assert headers > 0

    # goldsets without sibling structure never grow a header
    plain = build_modtext_examples(_synthetic_goldset(), samples=40, seed=42)
    assert all(
        SIBLING_CONTEXT_HEADER not in ex.context
        for examples in plain.values()
        for ex in examples
    )


def test_modtext_split_hygiene_and_invariants() -> None:
    goldset = _synthetic_goldset()
    split = build_modtext_examples(goldset, samples=40, batch_size=4, seed=42)
    assert set(split) == {"train", "val", "test"}

    assignment: dict[str, str] = {}
    for name, examples in split.items():
        for ex in examples:
            assert ex.stratum == "modtext"
            assert ex.glossary == "" and list(ex.term_rules) == []
            assert ex.context.startswith("Synth Pack modpack — ")
            for key, source in ex.entries.items():
                assert assignment.setdefault(key, name) == name, key
                # protected source tokens mirror exactly into the gold
                assert Counter(PH_RE.findall(source)) == Counter(
                    PH_RE.findall(ex.translations[key])
                )
    counts = {name: sum(len(ex.entries) for ex in split[name]) for name in split}
    assert counts == {"train": 24, "val": 8, "test": 8}

    # the key->split assignment must not depend on the sample count
    smaller = build_modtext_examples(goldset, samples=20, batch_size=4, seed=42)
    for name, examples in smaller.items():
        for ex in examples:
            for key in ex.entries:
                assert assignment[key] == name


def test_build_evalset_merges_matching_goldsets_only(tmp_path: Path) -> None:
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(_synthetic_goldset()), encoding="utf-8")

    split = build_evalset(
        vanilla_samples=32, modtext_paths=[path], modtext_samples=20, seed=42
    )
    strata = {ex.stratum for name in ("train", "val", "test") for ex in split[name]}
    assert "modtext" in strata

    ja = build_evalset(
        pairs=[("en_us", "ja_jp")],
        vanilla_samples=32,
        modtext_paths=[path],
        modtext_samples=20,
        seed=42,
    )
    assert not any(
        ex.stratum == "modtext" for name in ("train", "val", "test") for ex in ja[name]
    )
