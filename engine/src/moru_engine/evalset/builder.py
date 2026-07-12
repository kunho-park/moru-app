"""Evalset v0 builder.

Sources:
- vanilla official translations (assets/vanilla_minecraft_assets) — gold
  standard for style/terminology
- handcrafted stress cases (evalset/data/stress_cases.json)
- (flywheel, once the web platform ships) approved community corrections
  via GET /api/export/corrections

Split: train 60 / val 20 / test 20, deterministic. The test split must
NEVER be passed to an optimizer.
"""

from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path
from typing import TYPE_CHECKING

import dspy

from ..placeholder import PlaceholderProtector

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

_PKG_ROOT = Path(__file__).resolve().parents[1]
VANILLA_ASSETS_DIR = _PKG_ROOT / "assets" / "vanilla_minecraft_assets" / "versions"
VANILLA_GLOSSARY_DIR = _PKG_ROOT / "glossary" / "vanilla_glossaries"
STRESS_CASES_PATH = Path(__file__).resolve().parent / "data" / "stress_cases.json"

DEFAULT_VANILLA_SAMPLES = 400
DEFAULT_BATCH_SIZE = 8
DEFAULT_SEED = 42
MAX_RULES_PER_EXAMPLE = 25

INPUT_FIELDS = ("source_lang", "target_lang", "context", "glossary", "entries")


def _protect_pair(source: str, gold: str) -> tuple[str, str]:
    """Protect the source; mirror the same tokens into the gold reference."""
    protected = PlaceholderProtector().protect(source)
    gold_protected = gold
    for info in sorted(
        protected.placeholders, key=lambda p: len(p.original), reverse=True
    ):
        gold_protected = gold_protected.replace(info.original, info.token, 1)
    return protected.protected, gold_protected


def _alias_in_texts(alias: str, texts_lower: str) -> bool:
    return (
        re.search(
            r"(?<![a-z0-9_])" + re.escape(alias.lower()) + r"(?![a-z0-9_])",
            texts_lower,
        )
        is not None
    )


def _load_vanilla_rules(source_lang: str, target_lang: str) -> list[dict[str, object]]:
    path = VANILLA_GLOSSARY_DIR / f"vanilla_glossary_{source_lang}_{target_lang}.json"
    if not path.exists():
        logger.warning("No vanilla glossary for %s-%s", source_lang, target_lang)
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rules: list[dict[str, object]] = []
    for rule in data.get("term_rules", []):
        aliases = [a for a in rule.get("aliases", []) if a]
        target = rule.get("term_ko", "")
        if aliases and target:
            rules.append({"aliases": aliases, "target": target})
    return rules


def _select_rules(
    sources: Sequence[str],
    all_rules: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    joined = "\n".join(sources).lower()
    selected: list[dict[str, object]] = []
    for rule in all_rules:
        aliases = rule["aliases"]
        assert isinstance(aliases, list)
        if any(_alias_in_texts(str(a), joined) for a in aliases):
            selected.append(dict(rule))
            if len(selected) >= MAX_RULES_PER_EXAMPLE:
                break
    return selected


def _render_glossary(rules: Sequence[Mapping[str, object]]) -> str:
    lines: list[str] = []
    for rule in rules:
        aliases = rule["aliases"]
        assert isinstance(aliases, list)
        for alias in aliases:
            lines.append(f"{alias} = {rule['target']}")
    return "\n".join(lines)


def _make_example(
    *,
    source_lang: str,
    target_lang: str,
    context: str,
    entries: dict[str, str],
    translations: dict[str, str],
    term_rules: list[dict[str, object]],
) -> dspy.Example:
    return dspy.Example(
        source_lang=source_lang,
        target_lang=target_lang,
        context=context,
        glossary=_render_glossary(term_rules),
        entries=entries,
        translations=translations,
        term_rules=term_rules,
    ).with_inputs(*INPUT_FIELDS)


def build_vanilla_examples(
    source_lang: str = "en_us",
    target_lang: str = "ko_kr",
    *,
    minecraft_version: str = "1.21.5",
    samples: int = DEFAULT_VANILLA_SAMPLES,
    batch_size: int = DEFAULT_BATCH_SIZE,
    seed: int = DEFAULT_SEED,
) -> list[dspy.Example]:
    """Sample vanilla official translation pairs into batch examples."""
    version_dir = VANILLA_ASSETS_DIR / minecraft_version
    source_map = json.loads(
        (version_dir / f"{source_lang}.json").read_text(encoding="utf-8")
    )
    target_map = json.loads(
        (version_dir / f"{target_lang}.json").read_text(encoding="utf-8")
    )
    protector = PlaceholderProtector()
    shared = [
        k
        for k in sorted(set(source_map) & set(target_map))
        if len(source_map[k].strip()) >= 2
        and not protector.is_only_placeholders(protector.protect(source_map[k]))
    ]
    rng = random.Random(seed)
    keys = rng.sample(shared, min(samples, len(shared)))
    all_rules = _load_vanilla_rules(source_lang, target_lang)

    examples: list[dspy.Example] = []
    for start in range(0, len(keys), batch_size):
        chunk = keys[start : start + batch_size]
        entries: dict[str, str] = {}
        translations: dict[str, str] = {}
        for key in chunk:
            protected_source, protected_gold = _protect_pair(
                source_map[key], target_map[key]
            )
            entries[key] = protected_source
            translations[key] = protected_gold
        term_rules = _select_rules(list(entries.values()), all_rules)
        examples.append(
            _make_example(
                source_lang=source_lang,
                target_lang=target_lang,
                context="Minecraft vanilla UI text (official translation style)",
                entries=entries,
                translations=translations,
                term_rules=term_rules,
            )
        )
    logger.info("Built %d vanilla examples (%d entries)", len(examples), len(keys))
    return examples


def build_stress_examples(
    *,
    batch_size: int = 6,
    path: Path | None = None,
) -> list[dspy.Example]:
    """Load handcrafted stress cases, grouped per category."""
    data = json.loads((path or STRESS_CASES_PATH).read_text(encoding="utf-8"))
    source_lang = data["source_lang"]
    target_lang = data["target_lang"]
    by_category: dict[str, list[dict[str, object]]] = {}
    for case in data["cases"]:
        by_category.setdefault(str(case["category"]), []).append(case)

    examples: list[dspy.Example] = []
    for category, cases in sorted(by_category.items()):
        for start in range(0, len(cases), batch_size):
            chunk = cases[start : start + batch_size]
            entries: dict[str, str] = {}
            translations: dict[str, str] = {}
            term_rules: list[dict[str, object]] = []
            for case in chunk:
                protected_source, protected_gold = _protect_pair(
                    str(case["source"]), str(case["gold"])
                )
                entries[str(case["key"])] = protected_source
                translations[str(case["key"])] = protected_gold
                for rule in case.get("term_rules", []):  # type: ignore[union-attr]
                    if rule not in term_rules:
                        term_rules.append(rule)
            examples.append(
                _make_example(
                    source_lang=source_lang,
                    target_lang=target_lang,
                    context=f"Minecraft modpack text, stress category: {category}",
                    entries=entries,
                    translations=translations,
                    term_rules=term_rules,
                )
            )
    logger.info("Built %d stress examples", len(examples))
    return examples


def build_evalset(
    source_lang: str = "en_us",
    target_lang: str = "ko_kr",
    *,
    vanilla_samples: int = DEFAULT_VANILLA_SAMPLES,
    seed: int = DEFAULT_SEED,
) -> dict[str, list[dspy.Example]]:
    """Build the v0 evalset and split train/val/test 60/20/20.

    The split is deterministic for a given seed. Corrections-based examples
    (flywheel) are appended by tools/build_evalset.py once the web API is
    live.
    """
    examples = build_vanilla_examples(
        source_lang, target_lang, samples=vanilla_samples, seed=seed
    )
    if source_lang == "en_us" and target_lang == "ko_kr":
        examples += build_stress_examples()
    rng = random.Random(seed)
    rng.shuffle(examples)
    n = len(examples)
    n_train = int(n * 0.6)
    n_val = int(n * 0.2)
    split = {
        "train": examples[:n_train],
        "val": examples[n_train : n_train + n_val],
        "test": examples[n_train + n_val :],
    }
    logger.info(
        "Evalset split: train=%d val=%d test=%d",
        len(split["train"]),
        len(split["val"]),
        len(split["test"]),
    )
    return split
