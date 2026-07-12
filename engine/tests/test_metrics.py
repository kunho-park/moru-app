"""Metric component behavior: score direction and feedback specificity."""

from __future__ import annotations

import dspy

from moru_engine.evalset.builder import INPUT_FIELDS
from moru_engine.evalset.metrics import (
    format_component,
    glossary_component,
    make_metric,
    placeholder_component,
)


def _example(entries: dict[str, str], translations: dict[str, str], term_rules=None):
    return dspy.Example(
        source_lang="en_us",
        target_lang="ko_kr",
        context="test",
        glossary="",
        entries=entries,
        translations=translations,
        term_rules=term_rules or [],
    ).with_inputs(*INPUT_FIELDS)


def test_placeholder_component_flags_dropped_token() -> None:
    entries = {"k": "Hello {{PH1}} world {{PH2}}"}
    score, feedback = placeholder_component(entries, {"k": "안녕 {{PH1}} 세계"})
    assert score == 0.0
    assert "{{PH2}}" in feedback[0]


def test_placeholder_component_order_free() -> None:
    entries = {"k": "{{PH1}} before {{PH2}}"}
    score, feedback = placeholder_component(entries, {"k": "{{PH2}} 뒤에 {{PH1}}"})
    assert score == 1.0
    assert feedback == []


def test_glossary_component_enforces_terms() -> None:
    rules = [{"aliases": ["Enchanting Table"], "target": "마법 부여대"}]
    entries = {"k": "Craft an Enchanting Table"}
    good, _ = glossary_component(entries, {"k": "마법 부여대를 제작하세요"}, rules)
    bad, feedback = glossary_component(entries, {"k": "인챈팅 테이블을 제작하세요"}, rules)
    assert good == 1.0
    assert bad == 0.0
    assert "마법 부여대" in feedback[0]


def test_glossary_component_word_boundary() -> None:
    # "RF" must not match inside "PERFect"
    rules = [{"aliases": ["RF"], "target": "RF 에너지"}]
    entries = {"k": "A PERFect day"}
    score, feedback = glossary_component(entries, {"k": "완벽한 하루"}, rules)
    assert score == 1.0
    assert feedback == []


def test_format_component_flags_english_parens() -> None:
    entries = {"k": "Gain Experience points"}
    score, feedback = format_component(
        entries, {"k": "경험치 (Experience) 획득"}, "ko_kr"
    )
    assert score == 0.0
    assert "Experience" in feedback[0]


def test_format_component_accepts_clean_korean() -> None:
    entries = {"k": "Gain Experience points"}
    score, feedback = format_component(entries, {"k": "경험치 포인트 획득"}, "ko_kr")
    assert score == 1.0


def test_metric_perfect_scores_one() -> None:
    ex = _example(
        {"a": "Hello {{PH1}}"},
        {"a": "안녕하세요 {{PH1}}"},
    )
    metric = make_metric()
    result = metric(ex, dspy.Prediction(translations={"a": "안녕하세요 {{PH1}}"}))
    assert result.score == 1.0
    assert "All checks passed" in result.feedback


def test_metric_includes_gold_reference_on_failure() -> None:
    ex = _example(
        {"a": "Hello {{PH1}}"},
        {"a": "안녕하세요 {{PH1}}"},
    )
    metric = make_metric()
    result = metric(ex, dspy.Prediction(translations={"a": "hello"}))
    assert result.score < 1.0
    assert "Reference translation" in result.feedback
