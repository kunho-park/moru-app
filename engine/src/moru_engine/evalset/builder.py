"""Evalset builder (multi-pair, key-level split, dual batch strata).

Sources:
- vanilla official translations (assets/vanilla_minecraft_assets) — the
  gold standard for style/terminology. NO glossary input is attached to
  these examples: for single-term entries a vanilla term rule's target IS
  the gold translation, so rendering it into the prompt would hand the
  answer to the model and reward copying over translating.
- handcrafted stress cases (evalset/data/stress_cases.json) — carry their
  own binding term_rules (fragment constraints inside novel sentences);
  glossary compliance is measured here.
- (flywheel, once the web platform ships) approved community corrections
  via GET /api/export/corrections.

Split: translatable keys are split 60/20/20 (train/val/test) BEFORE any
example construction, with one deterministic assignment shared by every
language pair, so a lang key never crosses splits in any pair. The test
split must NEVER be passed to an optimizer.

Strata:
- narrow (batch_size=6 by default): many small examples — statistical
  resolution for metrics, Pareto tracking, and the paired adoption gate.
- wide (production packing): entries packed by the exact rule the runtime
  orchestrator uses (moru_engine.batching.pack_batches, 30 entries /
  8000 chars), so coverage and placeholder integrity are measured under
  production batch pressure.
"""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from typing import TYPE_CHECKING

import dspy

from ..batching import pack_batches
from ..placeholder import PlaceholderProtector

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

_PKG_ROOT = Path(__file__).resolve().parents[1]
VANILLA_ASSETS_DIR = _PKG_ROOT / "assets" / "vanilla_minecraft_assets" / "versions"
STRESS_CASES_PATH = Path(__file__).resolve().parent / "data" / "stress_cases.json"

DEFAULT_MINECRAFT_VERSION = "1.21.5"
DEFAULT_VANILLA_SAMPLES = 400
DEFAULT_BATCH_SIZE = 6
DEFAULT_SEED = 42

SPLIT_FRACTIONS = {"train": 0.6, "val": 0.2, "test": 0.2}
VANILLA_CONTEXT = "Minecraft vanilla UI text (official translation style)"

INPUT_FIELDS = ("source_lang", "target_lang", "context", "glossary", "entries")

DEFAULT_PAIRS = (("en_us", "ko_kr"),)


def _protect_pair(source: str, gold: str) -> tuple[str, str]:
    """Protect the source; mirror the same tokens into the gold reference."""
    protected = PlaceholderProtector().protect(source)
    gold_protected = gold
    for info in sorted(
        protected.placeholders, key=lambda p: len(p.original), reverse=True
    ):
        gold_protected = gold_protected.replace(info.original, info.token, 1)
    return protected.protected, gold_protected


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
    stratum: str,
) -> dspy.Example:
    return dspy.Example(
        source_lang=source_lang,
        target_lang=target_lang,
        context=context,
        glossary=_render_glossary(term_rules),
        entries=entries,
        translations=translations,
        term_rules=term_rules,
        stratum=stratum,
    ).with_inputs(*INPUT_FIELDS)


def slice_pair(
    examples: Sequence[dspy.Example], pair: tuple[str, str]
) -> list[dspy.Example]:
    """Examples belonging to one (source_lang, target_lang) pair."""
    return [ex for ex in examples if (ex.source_lang, ex.target_lang) == pair]


def _load_locale_maps(
    source_lang: str, target_lang: str, minecraft_version: str
) -> tuple[dict[str, str], dict[str, str]]:
    version_dir = VANILLA_ASSETS_DIR / minecraft_version
    source_map = json.loads(
        (version_dir / f"{source_lang}.json").read_text(encoding="utf-8")
    )
    target_map = json.loads(
        (version_dir / f"{target_lang}.json").read_text(encoding="utf-8")
    )
    return source_map, target_map


def _translatable_keys(
    source_map: Mapping[str, str], target_map: Mapping[str, str]
) -> list[str]:
    protector = PlaceholderProtector()
    return [
        k
        for k in sorted(set(source_map) & set(target_map))
        if len(source_map[k].strip()) >= 2
        and not protector.is_only_placeholders(protector.protect(source_map[k]))
    ]


def _split_keys(keys: Sequence[str], seed: int) -> dict[str, list[str]]:
    """Deterministic 60/20/20 key split, identical for identical key sets.

    Language pairs share the vanilla key namespace, so one assignment
    keeps every key in the same split across all pairs.
    """
    shuffled = list(keys)
    random.Random(seed).shuffle(shuffled)
    n = len(shuffled)
    n_train = int(n * SPLIT_FRACTIONS["train"])
    n_val = int(n * SPLIT_FRACTIONS["val"])
    return {
        "train": shuffled[:n_train],
        "val": shuffled[n_train : n_train + n_val],
        "test": shuffled[n_train + n_val :],
    }


def _protected_maps(
    source_map: Mapping[str, str],
    target_map: Mapping[str, str],
    keys: Sequence[str],
) -> tuple[dict[str, str], dict[str, str]]:
    entries: dict[str, str] = {}
    golds: dict[str, str] = {}
    for key in keys:
        protected_source, protected_gold = _protect_pair(
            source_map[key], target_map[key]
        )
        entries[key] = protected_source
        golds[key] = protected_gold
    return entries, golds


def _examples_from_batches(
    source_lang: str,
    target_lang: str,
    entries_map: Mapping[str, str],
    golds_map: Mapping[str, str],
    key_batches: Sequence[Sequence[str]],
    *,
    stratum: str,
) -> list[dspy.Example]:
    return [
        _make_example(
            source_lang=source_lang,
            target_lang=target_lang,
            context=VANILLA_CONTEXT,
            entries={k: entries_map[k] for k in chunk},
            translations={k: golds_map[k] for k in chunk},
            term_rules=[],
            stratum=stratum,
        )
        for chunk in key_batches
        if chunk
    ]


def build_vanilla_examples(
    source_lang: str = "en_us",
    target_lang: str = "ko_kr",
    *,
    minecraft_version: str = DEFAULT_MINECRAFT_VERSION,
    samples: int = DEFAULT_VANILLA_SAMPLES,
    batch_size: int = DEFAULT_BATCH_SIZE,
    seed: int = DEFAULT_SEED,
) -> list[dspy.Example]:
    """Sample vanilla official translation pairs into narrow batch examples.

    Ad-hoc helper without split hygiene — build_evalset is the split-aware
    entry point.
    """
    source_map, target_map = _load_locale_maps(
        source_lang, target_lang, minecraft_version
    )
    shared = _translatable_keys(source_map, target_map)
    rng = random.Random(seed)
    keys = rng.sample(shared, min(samples, len(shared)))
    entries_map, golds_map = _protected_maps(source_map, target_map, keys)
    batches = [keys[i : i + batch_size] for i in range(0, len(keys), batch_size)]
    examples = _examples_from_batches(
        source_lang, target_lang, entries_map, golds_map, batches, stratum="narrow"
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
                    stratum="stress",
                )
            )
    logger.info("Built %d stress examples", len(examples))
    return examples


def build_evalset(
    source_lang: str = "en_us",
    target_lang: str = "ko_kr",
    *,
    pairs: Sequence[tuple[str, str]] | None = None,
    vanilla_samples: int = DEFAULT_VANILLA_SAMPLES,
    wide_samples: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    seed: int = DEFAULT_SEED,
    minecraft_version: str = DEFAULT_MINECRAFT_VERSION,
) -> dict[str, list[dspy.Example]]:
    """Build the evalset and split train/val/test 60/20/20 at the KEY level.

    Args:
        pairs: language pairs to include; overrides source_lang/target_lang.
        vanilla_samples: narrow-strata entries sampled per pair.
        wide_samples: production-packed strata entries per pair
            (default: vanilla_samples // 3).
        batch_size: narrow-strata entries per example.

    Keys are bucketed into splits before example construction and the
    assignment is shared across pairs, so no key crosses splits anywhere.
    Corrections-based examples (flywheel) are appended by
    tools/build_evalset.py once the web API is live.
    """
    pair_list = [tuple(p) for p in pairs] if pairs else [(source_lang, target_lang)]
    if wide_samples is None:
        wide_samples = vanilla_samples // 3

    split: dict[str, list[dspy.Example]] = {"train": [], "val": [], "test": []}
    for src, tgt in pair_list:
        source_map, target_map = _load_locale_maps(src, tgt, minecraft_version)
        keys = _translatable_keys(source_map, target_map)
        buckets = _split_keys(keys, seed)
        for name, bucket in buckets.items():
            frac = SPLIT_FRACTIONS[name]
            n_narrow = int(round(vanilla_samples * frac))
            n_wide = int(round(wide_samples * frac))
            take = bucket[: min(len(bucket), n_narrow + n_wide)]
            narrow_keys = sorted(take[:n_narrow])
            wide_keys = sorted(take[n_narrow:])
            entries_map, golds_map = _protected_maps(source_map, target_map, take)
            narrow_batches: list[Sequence[str]] = [
                narrow_keys[i : i + batch_size]
                for i in range(0, len(narrow_keys), batch_size)
            ]
            wide_batches = [
                list(batch)
                for batch in pack_batches({k: entries_map[k] for k in wide_keys})
            ]
            split[name].extend(
                _examples_from_batches(
                    src, tgt, entries_map, golds_map, narrow_batches, stratum="narrow"
                )
            )
            split[name].extend(
                _examples_from_batches(
                    src, tgt, entries_map, golds_map, wide_batches, stratum="wide"
                )
            )

    if ("en_us", "ko_kr") in pair_list:
        stress = build_stress_examples()
        rng = random.Random(seed + 7)
        rng.shuffle(stress)
        n_train = int(len(stress) * SPLIT_FRACTIONS["train"])
        n_val = int(len(stress) * SPLIT_FRACTIONS["val"])
        split["train"].extend(stress[:n_train])
        split["val"].extend(stress[n_train : n_train + n_val])
        split["test"].extend(stress[n_train + n_val :])

    for index, name in enumerate(("train", "val", "test")):
        random.Random(seed * 1000 + index).shuffle(split[name])

    logger.info(
        "Evalset split (%s): train=%d val=%d test=%d examples",
        ", ".join(f"{s}-{t}" for s, t in pair_list),
        len(split["train"]),
        len(split["val"]),
        len(split["test"]),
    )
    return split
