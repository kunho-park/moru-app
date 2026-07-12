"""Judge protocols: position randomization mapping, clamping, failure paths."""

from __future__ import annotations

import dspy
import pytest

from moru_engine.evalset.builder import INPUT_FIELDS
from moru_engine.evalset.judge import LLMJudge, PairwiseJudge


class StubPairJudge:
    """Records slot contents; returns fixed slot scores."""

    def __init__(self, score_a: float, score_b: float, fail_times: int = 0):
        self.score_a = score_a
        self.score_b = score_b
        self.fail_times = fail_times
        self.calls: list[dict[str, str]] = []

    def __call__(self, **kwargs):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ValueError("boom")
        self.calls.append(kwargs)
        return dspy.Prediction(
            verdict="stub", score_a=self.score_a, score_b=self.score_b
        )


def _pairwise(stub: StubPairJudge) -> PairwiseJudge:
    judge = PairwiseJudge(dspy.utils.DummyLM([]))
    judge.judge = stub  # type: ignore[assignment]
    return judge


def _keys_by_swap() -> tuple[tuple[str, str], tuple[str, str]]:
    """One (key, source) with swap False and one with swap True."""
    unswapped = swapped = None
    for i in range(64):
        key, source = f"k{i}", f"source {i}"
        if PairwiseJudge.swap_slots(key, source):
            swapped = swapped or (key, source)
        else:
            unswapped = unswapped or (key, source)
        if swapped and unswapped:
            return unswapped, swapped
    raise AssertionError("crc32 coin never flipped in 64 tries")


def test_pairwise_maps_slots_back_through_swap() -> None:
    (uk, us), (sk, ss) = _keys_by_swap()

    # unswapped: baseline sits in slot A
    stub = StubPairJudge(score_a=2, score_b=8)
    result = _pairwise(stub).compare(
        key=uk, source=us, reference="ref", target_lang="ko_kr",
        baseline="base-text", candidate="cand-text",
    )
    assert result == pytest.approx((0.2, 0.8))
    assert stub.calls[0]["translation_a"] == "base-text"
    assert stub.calls[0]["translation_b"] == "cand-text"

    # swapped: baseline sits in slot B -> scores map back
    stub = StubPairJudge(score_a=2, score_b=8)
    result = _pairwise(stub).compare(
        key=sk, source=ss, reference="ref", target_lang="ko_kr",
        baseline="base-text", candidate="cand-text",
    )
    assert result == pytest.approx((0.8, 0.2))
    assert stub.calls[0]["translation_a"] == "cand-text"
    assert stub.calls[0]["translation_b"] == "base-text"


def test_pairwise_swap_assignment_is_roughly_balanced() -> None:
    flips = sum(
        PairwiseJudge.swap_slots(f"key{i}", f"text {i}") for i in range(200)
    )
    assert 60 <= flips <= 140


def test_pairwise_clamps_out_of_range_scores() -> None:
    (uk, us), _ = _keys_by_swap()
    stub = StubPairJudge(score_a=15, score_b=-3)
    result = _pairwise(stub).compare(
        key=uk, source=us, reference="ref", target_lang="ko_kr",
        baseline="b", candidate="c",
    )
    assert result == pytest.approx((1.0, 0.0))


def test_pairwise_retries_once_then_gives_up() -> None:
    (uk, us), _ = _keys_by_swap()
    # first attempt fails, second succeeds
    stub = StubPairJudge(score_a=5, score_b=5, fail_times=1)
    result = _pairwise(stub).compare(
        key=uk, source=us, reference="ref", target_lang="ko_kr",
        baseline="b", candidate="c",
    )
    assert result == pytest.approx((0.5, 0.5))

    # both attempts fail -> None
    stub = StubPairJudge(score_a=5, score_b=5, fail_times=2)
    result = _pairwise(stub).compare(
        key=uk, source=us, reference="ref", target_lang="ko_kr",
        baseline="b", candidate="c",
    )
    assert result is None


class StubQualityJudge:
    def __call__(self, **kwargs):
        return dspy.Prediction(score=0.5, issues="")


def test_absolute_judge_averages_and_zeroes_missing() -> None:
    judge = LLMJudge(dspy.utils.DummyLM([]))
    judge.judge = StubQualityJudge()  # type: ignore[assignment]
    gold = dspy.Example(
        source_lang="en_us",
        target_lang="ko_kr",
        context="test",
        glossary="",
        entries={"a": "Hello", "b": "World"},
        translations={"a": "안녕", "b": "세계"},
        term_rules=[],
    ).with_inputs(*INPUT_FIELDS)
    pred = dspy.Prediction(translations={"a": "안녕"})  # b missing -> 0.0
    score, issues = judge(gold, pred)
    assert score == pytest.approx(0.25)
