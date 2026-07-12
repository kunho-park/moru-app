"""Evalset builder: determinism, split hygiene, strata, token mirroring."""

from __future__ import annotations

import re
from collections import Counter

from moru_engine.batching import DEFAULT_BATCH_SIZE, DEFAULT_MAX_BATCH_CHARS
from moru_engine.evalset import build_evalset, build_stress_examples

PH_RE = re.compile(r"\{\{[A-Z]+\d*\}\}")

PAIRS = [("en_us", "ko_kr"), ("en_us", "ja_jp"), ("en_us", "zh_cn")]


def _ids(examples) -> list[tuple[str, ...]]:
    return [tuple(sorted(ex.entries.keys())) for ex in examples]


def test_split_deterministic_and_disjoint() -> None:
    a = build_evalset(vanilla_samples=64, seed=42)
    b = build_evalset(vanilla_samples=64, seed=42)
    assert _ids(a["train"]) == _ids(b["train"])
    assert _ids(a["test"]) == _ids(b["test"])

    train_keys = {k for ex in a["train"] for k in ex.entries}
    test_keys = {k for ex in a["test"] for k in ex.entries}
    assert train_keys.isdisjoint(test_keys)


def test_examples_have_inputs_and_gold() -> None:
    split = build_evalset(vanilla_samples=32, seed=7)
    for name in ("train", "val", "test"):
        for ex in split[name]:
            assert set(ex.inputs().keys()) == {
                "source_lang",
                "target_lang",
                "context",
                "glossary",
                "entries",
            }
            assert ex.entries and ex.translations
            assert set(ex.entries) == set(ex.translations)


def test_multi_pair_key_split_is_consistent() -> None:
    """A vanilla key must live in the same split for every language pair."""
    split = build_evalset(pairs=PAIRS, vanilla_samples=96, seed=42)
    assignment: dict[str, str] = {}
    for name in ("train", "val", "test"):
        for ex in split[name]:
            if getattr(ex, "stratum", None) == "stress":
                continue
            for key in ex.entries:
                assert assignment.setdefault(key, name) == name, key


def test_multi_pair_covers_all_pairs_in_every_split() -> None:
    split = build_evalset(pairs=PAIRS, vanilla_samples=96, seed=42)
    for name in ("train", "val", "test"):
        seen = {(ex.source_lang, ex.target_lang) for ex in split[name]}
        assert set(PAIRS) <= seen, name


def test_vanilla_examples_carry_no_glossary() -> None:
    """Glossary targets equal gold translations for term entries — leaking
    them into the prompt would let candidates score by copying."""
    split = build_evalset(vanilla_samples=48, seed=42)
    for name in ("train", "val", "test"):
        for ex in split[name]:
            if getattr(ex, "stratum", None) in ("narrow", "wide"):
                assert ex.glossary == ""
                assert list(ex.term_rules) == []


def test_wide_stratum_respects_production_packing() -> None:
    split = build_evalset(vanilla_samples=300, wide_samples=120, seed=42)
    wide = [
        ex
        for name in ("train", "val", "test")
        for ex in split[name]
        if getattr(ex, "stratum", None) == "wide"
    ]
    assert wide, "wide stratum missing"
    saw_large_batch = False
    for ex in wide:
        assert len(ex.entries) <= DEFAULT_BATCH_SIZE
        chars = sum(len(t) for t in ex.entries.values())
        assert len(ex.entries) == 1 or chars <= DEFAULT_MAX_BATCH_CHARS
        saw_large_batch = saw_large_batch or len(ex.entries) > 10
    assert saw_large_batch, "wide stratum never packed a production-sized batch"


def test_stress_examples_stratified_across_splits() -> None:
    split = build_evalset(vanilla_samples=48, seed=42)
    counts = {
        name: sum(
            1 for ex in split[name] if getattr(ex, "stratum", None) == "stress"
        )
        for name in ("train", "val", "test")
    }
    assert all(count > 0 for count in counts.values()), counts


def test_stress_cases_token_multisets_match_exactly() -> None:
    """Every stress entry's protected source and gold must carry the SAME
    token multiset — a divergent case (e.g. raw JSON masked on one side
    only) is structurally unsolvable for the model and poisons the metric,
    the reflective feedback, and the judge."""
    examples = build_stress_examples()
    assert examples
    tokened = 0
    for ex in examples:
        for key, source in ex.entries.items():
            src_tokens = Counter(PH_RE.findall(source))
            gold_tokens = Counter(PH_RE.findall(ex.translations[key]))
            assert src_tokens == gold_tokens, (
                f"{key}: source tokens {dict(src_tokens)} != "
                f"gold tokens {dict(gold_tokens)}"
            )
            if src_tokens:
                tokened += 1
    # the stress set stays token-heavy by design
    assert tokened >= 15
