"""Adoption gate: bootstrap CI, decision rules, integrity zero tolerance."""

from __future__ import annotations

import dspy
import pytest

from moru_engine.evalset.builder import INPUT_FIELDS
from moru_engine.evalset.gate import (
    cluster_bootstrap,
    decide,
    entry_integrity,
)


def _example(entries: dict[str, str], stratum: str = "narrow"):
    return dspy.Example(
        source_lang="en_us",
        target_lang="ko_kr",
        context="test",
        glossary="",
        entries=entries,
        translations={k: f"골드 {k}" for k in entries},
        term_rules=[],
        stratum=stratum,
    ).with_inputs(*INPUT_FIELDS)


def _pred(translations: dict[str, str]):
    return dspy.Prediction(translations=translations)


# --- cluster bootstrap ---------------------------------------------------


def test_cluster_bootstrap_constant_positive() -> None:
    result = cluster_bootstrap([[0.1, 0.1], [0.1], [0.1, 0.1]], iters=500, seed=1)
    assert result is not None
    mean, lo, hi = result
    assert mean == pytest.approx(0.1)
    assert lo == pytest.approx(0.1)
    assert hi == pytest.approx(0.1)


def test_cluster_bootstrap_ci_contains_mean() -> None:
    clusters = [[0.2], [-0.1], [0.3], [0.0], [0.1], [-0.05], [0.15], [0.25]]
    result = cluster_bootstrap(clusters, iters=2000, seed=7)
    assert result is not None
    mean, lo, hi = result
    assert lo <= mean <= hi
    assert lo < hi


def test_cluster_bootstrap_empty_returns_none() -> None:
    assert cluster_bootstrap([]) is None
    assert cluster_bootstrap([[], []]) is None


def test_cluster_bootstrap_deterministic_for_seed() -> None:
    clusters = [[0.2, -0.1], [0.05], [0.4, 0.0]]
    a = cluster_bootstrap(clusters, iters=500, seed=3)
    b = cluster_bootstrap(clusters, iters=500, seed=3)
    assert a == b


# --- integrity counting ---------------------------------------------------


def test_entry_integrity_counts_by_stratum() -> None:
    examples = [
        _example({"a": "Hello {{ARG}}"}, stratum="narrow"),
        _example({"b": "Bye {{ARG}}", "c": "Hi"}, stratum="wide"),
    ]
    preds = [
        _pred({"a": "안녕 {{ARG}}"}),
        _pred({"b": "잘 가"}),  # dropped token on b, missing c entirely
    ]
    stats = entry_integrity(examples, preds)
    assert stats["overall"].entries == 3
    assert stats["overall"].placeholder_failures == 2
    assert stats["overall"].coverage_misses == 1
    assert stats["wide"].placeholder_failures == 2
    assert stats["narrow"].placeholder_failures == 0


# --- decision rules --------------------------------------------------------


def _decide(
    *,
    examples,
    base_preds,
    cand_preds,
    judge_scores,
    base_scores=None,
    cand_scores=None,
    **kwargs,
):
    n = len(examples)
    return decide(
        pair="en_us-ko_kr",
        examples=examples,
        baseline_preds=base_preds,
        baseline_scores=base_scores or [0.8] * n,
        candidate_preds=cand_preds,
        candidate_scores=cand_scores or [0.85] * n,
        judge_scores=judge_scores,
        n_judge_tasks=sum(len(ex.entries) for ex in examples),
        **kwargs,
    )


def _clean_examples(n: int = 6):
    examples = [_example({f"k{i}": f"Hello {{{{ARG}}}} {i}"}) for i in range(n)]
    preds = [_pred({f"k{i}": f"안녕 {{{{ARG}}}} {i}"}) for i in range(n)]
    return examples, preds


def test_gate_adopts_confident_judge_win() -> None:
    examples, preds = _clean_examples()
    judge_scores = [[(0.6, 0.9)] for _ in examples]
    decision = _decide(
        examples=examples,
        base_preds=preds,
        cand_preds=preds,
        judge_scores=judge_scores,
    )
    assert decision.adopted
    assert decision.judge_delta == pytest.approx(0.3)
    assert decision.judge_wins == len(examples)


def test_gate_rejects_negative_judge_delta() -> None:
    examples, preds = _clean_examples()
    judge_scores = [[(0.9, 0.6)] for _ in examples]
    decision = _decide(
        examples=examples,
        base_preds=preds,
        cand_preds=preds,
        judge_scores=judge_scores,
    )
    assert not decision.adopted
    assert any("not positive" in r for r in decision.reasons)


def test_gate_rejects_inconclusive_ci() -> None:
    examples, preds = _clean_examples(8)
    # mean slightly positive but noisy -> CI lower bound below margin
    deltas = [0.4, -0.3, 0.35, -0.25, 0.3, -0.2, 0.25, -0.15]
    judge_scores = [[(0.5, 0.5 + d)] for d in deltas]
    decision = _decide(
        examples=examples,
        base_preds=preds,
        cand_preds=preds,
        judge_scores=judge_scores,
    )
    assert not decision.adopted
    assert any("CI lower bound" in r for r in decision.reasons)


def test_gate_zero_tolerance_for_wide_integrity_regression() -> None:
    # judge says candidate wins, but the candidate drops one token in the
    # wide stratum -> must be rejected.
    examples = [
        _example({"a": "Hello {{ARG}}"}, stratum="narrow"),
        _example({"b": "World {{ARG}}"}, stratum="wide"),
    ]
    base_preds = [_pred({"a": "안녕 {{ARG}}"}), _pred({"b": "세계 {{ARG}}"})]
    cand_preds = [_pred({"a": "안녕 {{ARG}}"}), _pred({"b": "세계"})]
    judge_scores = [[(0.5, 0.9)], [(0.5, 0.9)]]
    decision = _decide(
        examples=examples,
        base_preds=base_preds,
        cand_preds=cand_preds,
        judge_scores=judge_scores,
    )
    assert not decision.adopted
    assert any("wide" in r and "placeholder" in r for r in decision.reasons)


def test_gate_rejects_single_new_coverage_miss() -> None:
    examples = [_example({"a": "Hello", "b": "World"})]
    base_preds = [_pred({"a": "안녕", "b": "세계"})]
    cand_preds = [_pred({"a": "안녕"})]
    judge_scores = [[(0.5, 0.9), (0.5, 0.9)]]
    decision = _decide(
        examples=examples,
        base_preds=base_preds,
        cand_preds=cand_preds,
        judge_scores=judge_scores,
    )
    assert not decision.adopted
    assert any("coverage" in r for r in decision.reasons)


def test_gate_rejects_low_judge_coverage() -> None:
    examples, preds = _clean_examples(10)
    # only 5 of 10 entries judged -> below the 80% coverage requirement
    judge_scores = [[(0.5, 0.9)] if i < 5 else [] for i in range(10)]
    decision = _decide(
        examples=examples,
        base_preds=preds,
        cand_preds=preds,
        judge_scores=judge_scores,
    )
    assert not decision.adopted
    assert any("coverage" in r and "below" in r for r in decision.reasons)


def test_gate_rejects_deterministic_metric_regression() -> None:
    examples, preds = _clean_examples()
    judge_scores = [[(0.6, 0.9)] for _ in examples]
    decision = _decide(
        examples=examples,
        base_preds=preds,
        cand_preds=preds,
        judge_scores=judge_scores,
        base_scores=[0.9] * len(examples),
        cand_scores=[0.7] * len(examples),
    )
    assert not decision.adopted
    assert any("deterministic metric regressed" in r for r in decision.reasons)


def test_gate_without_judge_uses_metric_ci() -> None:
    examples, preds = _clean_examples()
    adopted = _decide(
        examples=examples,
        base_preds=preds,
        cand_preds=preds,
        judge_scores=None,
        base_scores=[0.7] * len(examples),
        cand_scores=[0.8] * len(examples),
    )
    assert adopted.adopted
    rejected = _decide(
        examples=examples,
        base_preds=preds,
        cand_preds=preds,
        judge_scores=None,
        base_scores=[0.8] * len(examples),
        cand_scores=[0.7] * len(examples),
    )
    assert not rejected.adopted


def test_gate_require_judge_blocks_adoption_without_judge() -> None:
    """Confirmatory adoption is defined as the pairwise-judge protocol;
    without a judge the deterministic CI may reject but never adopt."""
    examples, preds = _clean_examples()
    decision = _decide(
        examples=examples,
        base_preds=preds,
        cand_preds=preds,
        judge_scores=None,
        base_scores=[0.7] * len(examples),
        cand_scores=[0.8] * len(examples),
        require_judge=True,
    )
    assert not decision.adopted
    assert any("LLM judge required" in r for r in decision.reasons)
