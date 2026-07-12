"""Evalset builder: determinism, split hygiene, token mirroring."""

from __future__ import annotations

import re

from moru_engine.evalset import build_evalset, build_stress_examples

PH_RE = re.compile(r"\{\{[A-Z]+\d*\}\}")


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


def test_stress_cases_token_mirroring() -> None:
    examples = build_stress_examples()
    assert examples
    mirrored = 0
    for ex in examples:
        for key, source in ex.entries.items():
            src_tokens = set(PH_RE.findall(source))
            gold_tokens = set(PH_RE.findall(ex.translations[key]))
            # every gold token must come from the source protection map
            assert gold_tokens <= src_tokens, key
            if src_tokens and src_tokens == gold_tokens:
                mirrored += 1
    # stress set is token-heavy by design: most protected entries mirror fully.
    # (json_in_string cases legitimately diverge: the whole JSON body is
    # protected as one token on the source side only.)
    assert mirrored >= 15
