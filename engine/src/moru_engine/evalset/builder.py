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
- modpack goldsets (evalset/modtext.py) — sentence-level human
  translations harvested from packs that ship both locales: quest prose,
  NPC dialogue, Patchouli pages. The "modtext" stratum; glossary-free
  for the vanilla-strata reason.
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
import re
from pathlib import Path
from typing import TYPE_CHECKING

import dspy

from ..batching import pack_batches
from ..graph import SIBLING_CONTEXT_HEADER, SIBLING_FIELDS
from ..placeholder import PlaceholderProtector
from .modtext import load_goldset

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
DEFAULT_MODTEXT_SAMPLES = 900

SPLIT_FRACTIONS = {"train": 0.6, "val": 0.2, "test": 0.2}
VANILLA_CONTEXT = "Minecraft vanilla UI text (official translation style)"

INPUT_FIELDS = ("source_lang", "target_lang", "context", "glossary", "entries")

DEFAULT_PAIRS = (("en_us", "ko_kr"),)

#: Content-type hint per harvested category (the signature's context
#: field: "mod name, content type").
MODTEXT_CONTEXTS = {
    "quest": "FTB Quests quest text (titles, subtitles, long descriptions)",
    "dialog": "NPC dialogue lines (conversational tone and register)",
    "patchouli": "Patchouli guidebook pages ($(...) markup)",
    "lang": "item/block names, tooltips, UI messages",
}

#: Sibling-block shape mirrored from graph.sibling_context defaults; the
#: evalset must show GEPA the exact prompt surface production builds.
MODTEXT_SIBLING_MAX_LINES = 8
MODTEXT_SIBLING_MAX_CHARS = 800
_MODTEXT_FIELD_CHARS = 100
#: Trailing array index OR bare digit suffix on a key's last segment
#: ("description[3]", "description1"); graph.stem() only handles the
#: bracket form because SNBT keys carry indexes, while goldset lang keys
#: (ftbquestlocalizer) number their fields.
_MODTEXT_INDEX_RE = re.compile(r"(?:\[\d+\]|\d+)$")


def _protect_pair(source: str, gold: str) -> tuple[str, str]:
    """Protect the source; mirror the same tokens into the gold reference."""
    protected = PlaceholderProtector().protect(source)
    gold_protected = gold
    for info in sorted(
        protected.placeholders, key=lambda p: len(p.original), reverse=True
    ):
        gold_protected = gold_protected.replace(info.original, info.token, 1)
    return protected.protected, gold_protected


def _modtext_stem(key: str) -> str | None:
    """Sibling-group stem of a goldset key, or None for a standalone key.

    Patchouli page keys ("patchouli:book/entry#text0") group per book
    file; namespaced lang keys group when the digit/index-stripped last
    segment names a field of the parent object (graph.stem() semantics,
    extended to bare digit suffixes).
    """
    if "#" in key:
        return key.partition("#")[0]
    head, _, last = key.rpartition(".")
    if head and _MODTEXT_INDEX_RE.sub("", last).lower() in SIBLING_FIELDS:
        return head
    return None


def _flatten(text: str, limit: int = _MODTEXT_FIELD_CHARS) -> str:
    """Newlines to spaces, whitespace collapsed, truncated to ``limit``."""
    return " ".join(text.split())[:limit]


def _modtext_sibling_block(
    chunk: Sequence[str],
    group_of: Mapping[str, Sequence[str]],
    pairs_by_key: Mapping[str, Mapping[str, object]],
) -> str:
    """Production-shaped sibling block for one batch, or ``""``.

    Mirrors graph.sibling_context: walk the batch keys in order, quote
    same-split group members outside the batch as
    ``- key: "source" => "translated"`` lines (raw text, namespace
    stripped — production keys carry no namespace) until a cap hits.
    Quoting is restricted to the batch's own split bucket by
    construction, so no other split's gold ever leaks into an input.
    """
    exclude = set(chunk)
    lines: list[str] = []
    seen: set[str] = set()
    total = 0
    for key in chunk:
        for member in group_of.get(key, ()):
            if member in exclude or member in seen:
                continue
            seen.add(member)
            pair = pairs_by_key[member]
            display = member.partition(":")[2] or member
            line = (
                f'- {display}: "{_flatten(str(pair["source"]))}"'
                f' => "{_flatten(str(pair["target"]))}"'
            )
            if lines and total + len(line) > MODTEXT_SIBLING_MAX_CHARS:
                return "\n".join(lines)
            lines.append(line)
            total += len(line)
            if (
                len(lines) >= MODTEXT_SIBLING_MAX_LINES
                or total >= MODTEXT_SIBLING_MAX_CHARS
            ):
                return "\n".join(lines)
    return "\n".join(lines)


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


def parse_pair_spec(
    spec: str | None, source_lang: str = "en_us", target_lang: str = "ko_kr"
) -> list[tuple[str, str]]:
    """Parse a 'src:tgt,src:tgt' CLI spec; fall back to the single pair."""
    if not spec:
        return [(source_lang, target_lang)]
    pairs: list[tuple[str, str]] = []
    for chunk in spec.split(","):
        source, _, target = chunk.strip().partition(":")
        if not source or not target:
            raise ValueError(f"pair entry '{chunk}' must be 'source:target'")
        pairs.append((source, target))
    return pairs


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


def build_modtext_examples(
    goldset: Mapping[str, object],
    *,
    samples: int = DEFAULT_MODTEXT_SAMPLES,
    batch_size: int = DEFAULT_BATCH_SIZE,
    seed: int = DEFAULT_SEED,
) -> dict[str, list[dspy.Example]]:
    """Split one harvested goldset into "modtext"-stratum examples.

    Keys are bucketed 60/20/20 BEFORE sampling (vanilla-strata
    discipline: the assignment depends only on the goldset's full key
    set and the seed, never on the sample count), then ``samples *
    fraction`` keys are taken per bucket and batched per content
    category, so one example never mixes quest prose with dialogue and
    its context names the content type.

    The split unit is the sibling GROUP, not the key: a quest's title and
    description lines (or one book entry's pages) are correlated text, so
    letting them straddle splits would leak style/terminology across the
    boundary — and a group that arrives whole is what makes sibling
    context constructible. Groups taken into a split are rendered into
    batch contexts exactly the way the pipeline orchestrator does at
    runtime — the SIBLING_CONTEXT_HEADER line plus settled
    ``- key: "src" => "gold"`` lines quoting group members outside the
    batch — so GEPA optimizes against the production prompt surface. A
    sibling's gold is context, not the entry's answer, and never crosses
    split buckets.

    No glossary is attached, for the vanilla-strata reason: the gold IS
    the answer for term-like entries.
    """
    pairs_by_key: dict[str, Mapping[str, object]] = {
        str(p["key"]): p
        for p in goldset["pairs"]  # type: ignore[union-attr]
    }
    members_of: dict[str, list[str]] = {}
    for key in sorted(pairs_by_key):
        members_of.setdefault(_modtext_stem(key) or key, []).append(key)
    buckets = _split_keys(sorted(members_of), seed)
    pack = str(goldset["pack"])
    source_lang = str(goldset["source_lang"])
    target_lang = str(goldset["target_lang"])

    split: dict[str, list[dspy.Example]] = {}
    for name, bucket in buckets.items():
        budget = int(round(samples * SPLIT_FRACTIONS[name]))
        take: list[str] = []
        group_of: dict[str, list[str]] = {}
        for unit in bucket:
            if len(take) >= budget:
                break
            members = members_of[unit]
            take.extend(members)
            if len(members) >= 2:
                for member in members:
                    group_of[member] = members
        by_category: dict[str, list[str]] = {}
        for key in take:
            category = str(pairs_by_key[key]["category"])
            by_category.setdefault(category, []).append(key)
        examples: list[dspy.Example] = []
        for category, keys in sorted(by_category.items()):
            keys.sort()
            entries_map: dict[str, str] = {}
            golds_map: dict[str, str] = {}
            for key in keys:
                pair = pairs_by_key[key]
                entries_map[key], golds_map[key] = _protect_pair(
                    str(pair["source"]), str(pair["target"])
                )
            context = f"{pack} modpack — {MODTEXT_CONTEXTS.get(category, category)}"
            for start in range(0, len(keys), batch_size):
                chunk = keys[start : start + batch_size]
                block = _modtext_sibling_block(chunk, group_of, pairs_by_key)
                batch_context = (
                    f"{context}\n{SIBLING_CONTEXT_HEADER}\n{block}"
                    if block
                    else context
                )
                examples.append(
                    _make_example(
                        source_lang=source_lang,
                        target_lang=target_lang,
                        context=batch_context,
                        entries={k: entries_map[k] for k in chunk},
                        translations={k: golds_map[k] for k in chunk},
                        term_rules=[],
                        stratum="modtext",
                    )
                )
        split[name] = examples
    logger.info(
        "Built modtext examples from %s: %s",
        pack,
        ", ".join(f"{name}={len(examples)}" for name, examples in split.items()),
    )
    return split


def build_evalset(
    source_lang: str = "en_us",
    target_lang: str = "ko_kr",
    *,
    pairs: Sequence[tuple[str, str]] | None = None,
    vanilla_samples: int = DEFAULT_VANILLA_SAMPLES,
    wide_samples: int | None = None,
    confirmation_samples: int = 0,
    batch_size: int = DEFAULT_BATCH_SIZE,
    seed: int = DEFAULT_SEED,
    modtext_paths: Sequence[Path | str] | None = None,
    modtext_samples: int = DEFAULT_MODTEXT_SAMPLES,
    minecraft_version: str = DEFAULT_MINECRAFT_VERSION,
) -> dict[str, list[dspy.Example]]:
    """Build the evalset and split train/val/test 60/20/20 at the KEY level.

    Args:
        pairs: language pairs to include; overrides source_lang/target_lang.
        vanilla_samples: narrow-strata entries sampled per pair.
        wide_samples: production-packed strata entries per pair
            (default: vanilla_samples // 3).
        confirmation_samples: when > 0, adds a "confirmation" split of
            this many narrow entries (plus //3 production-packed) drawn
            from the test KEY BUCKET but strictly AFTER the keys the
            regular test sample consumes. It is key-disjoint from every
            other split INCLUDING the test examples, reserved for one
            final confirmatory gate of externally produced candidates
            (tools/gate.py --final) after optimize.py has already spent
            the test split on its own adoption decision.
        batch_size: narrow-strata entries per example.
        modtext_paths: harvested modpack goldset files
            (tools/harvest_modpack_pairs.py). Each contributes a
            "modtext" stratum to train/val/test when its language pair
            is requested; never to the confirmation split.
        modtext_samples: modtext entries taken per goldset.

    Keys are bucketed into splits before example construction and the
    assignment is shared across pairs, so no key crosses splits anywhere.
    Corrections-based examples (flywheel) are appended by
    tools/build_evalset.py once the web API is live.
    """
    pair_list = [tuple(p) for p in pairs] if pairs else [(source_lang, target_lang)]
    if wide_samples is None:
        wide_samples = vanilla_samples // 3

    split: dict[str, list[dspy.Example]] = {"train": [], "val": [], "test": []}
    if confirmation_samples > 0:
        split["confirmation"] = []
    for src, tgt in pair_list:
        source_map, target_map = _load_locale_maps(src, tgt, minecraft_version)
        keys = _translatable_keys(source_map, target_map)
        buckets = _split_keys(keys, seed)
        for name, bucket in buckets.items():
            frac = SPLIT_FRACTIONS[name]
            plan = [(name, int(round(vanilla_samples * frac)), int(round(wide_samples * frac)))]
            if name == "test" and confirmation_samples > 0:
                conf_narrow = confirmation_samples
                conf_wide = confirmation_samples // 3
                test_take = plan[0][1] + plan[0][2]
                needed = test_take + conf_narrow + conf_wide
                if len(bucket) < needed:
                    raise ValueError(
                        f"confirmation split for {src}-{tgt} needs "
                        f"{conf_narrow + conf_wide} keys AFTER the {test_take} "
                        f"test keys, but the test bucket holds only "
                        f"{len(bucket)}; lower confirmation_samples or "
                        f"vanilla_samples"
                    )
                plan.append(("confirmation", conf_narrow, conf_wide))
            offset = 0
            for split_name, n_narrow, n_wide in plan:
                take = bucket[offset : offset + min(len(bucket) - offset, n_narrow + n_wide)]
                offset += len(take)
                if not take:
                    continue
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
                split[split_name].extend(
                    _examples_from_batches(
                        src, tgt, entries_map, golds_map, narrow_batches, stratum="narrow"
                    )
                )
                split[split_name].extend(
                    _examples_from_batches(
                        src, tgt, entries_map, golds_map, wide_batches, stratum="wide"
                    )
                )

    for goldset_path in modtext_paths or ():
        goldset = load_goldset(goldset_path)
        goldset_pair = (str(goldset["source_lang"]), str(goldset["target_lang"]))
        if goldset_pair not in pair_list:
            logger.info(
                "Skipping goldset %s: pair %s-%s not requested",
                goldset_path,
                *goldset_pair,
            )
            continue
        modtext_split = build_modtext_examples(
            goldset, samples=modtext_samples, batch_size=batch_size, seed=seed
        )
        for name in ("train", "val", "test"):
            split[name].extend(modtext_split[name])

    if ("en_us", "ko_kr") in pair_list:
        stress = build_stress_examples()
        rng = random.Random(seed + 7)
        rng.shuffle(stress)
        n_train = int(len(stress) * SPLIT_FRACTIONS["train"])
        n_val = int(len(stress) * SPLIT_FRACTIONS["val"])
        split["train"].extend(stress[:n_train])
        split["val"].extend(stress[n_train : n_train + n_val])
        split["test"].extend(stress[n_train + n_val :])

    shuffle_salt = {"train": 0, "val": 1, "test": 2, "confirmation": 3}
    for name in split:
        random.Random(seed * 1000 + shuffle_salt[name]).shuffle(split[name])

    logger.info(
        "Evalset split (%s): %s examples",
        ", ".join(f"{s}-{t}" for s, t in pair_list),
        ", ".join(f"{name}={len(split[name])}" for name in split),
    )
    return split
