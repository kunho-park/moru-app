"""Metric behavior: chrF parity, score direction, weight renormalization,
and predictor-trace-grounded feedback."""

from __future__ import annotations

import dspy
import pytest

from moru_engine.evalset.builder import INPUT_FIELDS
from moru_engine.evalset.metrics import (
    W_FORMAT,
    W_PLACEHOLDER,
    W_SIMILARITY,
    chrf_score,
    format_component,
    glossary_component,
    make_metric,
    placeholder_component,
    similarity_component,
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


# --- chrF ---------------------------------------------------------------

# reference values computed with sacrebleu 2.x CHRF() defaults
# (char_order=6, word_order=0, beta=2, whitespace=False, eps_smoothing=False)
SACREBLEU_CASES = [
    ("고양이가 매트 위에 앉았다", "고양이가 매트 위에 앉아 있었다", 0.6462119885),
    ("hello world", "hello there world", 0.3990603146),
    ("ab", "abcdef", 0.3125),
    ("철 검", "철 검", 1.0),
    ("완전히 다른 문장", "totally different", 0.0),
    ("エメラルド 5個が必要です", "エメラルドが5個必要です", 0.4476010101),
    ("红石中继器", "红石中继器已充能", 0.5250119866),
]


@pytest.mark.parametrize(("hyp", "ref", "expected"), SACREBLEU_CASES)
def test_chrf_matches_sacrebleu(hyp: str, ref: str, expected: float) -> None:
    assert chrf_score(hyp, ref) == pytest.approx(expected, abs=1e-9)


def test_chrf_strips_protected_tokens() -> None:
    # identical tokens must not inflate similarity of divergent text
    with_tokens = chrf_score("{{COLOR}}완전히 다른{{RESET}}", "{{COLOR}}전혀 무관한{{RESET}}")
    without = chrf_score("완전히 다른", "전혀 무관한")
    assert with_tokens == pytest.approx(without)


def test_chrf_empty_behavior() -> None:
    assert chrf_score("", "") == 1.0
    assert chrf_score("", "안녕") == 0.0
    assert chrf_score("안녕", "") == 0.0


# --- components ---------------------------------------------------------


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
    good, _, good_checks = glossary_component(
        entries, {"k": "마법 부여대를 제작하세요"}, rules
    )
    bad, feedback, bad_checks = glossary_component(
        entries, {"k": "인챈팅 테이블을 제작하세요"}, rules
    )
    assert good == 1.0
    assert good_checks == 1
    assert bad == 0.0
    assert bad_checks == 1
    assert "마법 부여대" in feedback[0]


def test_glossary_component_word_boundary() -> None:
    # "RF" must not match inside "PERFect"
    rules = [{"aliases": ["RF"], "target": "RF 에너지"}]
    entries = {"k": "A PERFect day"}
    score, feedback, checks = glossary_component(entries, {"k": "완벽한 하루"}, rules)
    assert score == 1.0
    assert checks == 0
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


def test_similarity_component_scores_missing_as_zero() -> None:
    entries = {"a": "Iron Sword", "b": "Stone"}
    gold = {"a": "철 검", "b": "돌"}
    score, feedback = similarity_component(entries, {"a": "철 검"}, gold)
    assert score == pytest.approx(0.5)


def test_similarity_component_quotes_reference_for_worst_keys() -> None:
    entries = {"a": "Iron Sword"}
    gold = {"a": "철 검"}
    score, feedback = similarity_component(entries, {"a": "아이언 스워드"}, gold)
    assert score < 0.5
    assert feedback and "철 검" in feedback[0]


# --- metric -------------------------------------------------------------


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


def test_metric_renormalizes_without_glossary_checks() -> None:
    ex = _example({"a": "Iron Sword"}, {"a": "철 검"})
    metric = make_metric()
    pred = dspy.Prediction(translations={"a": "무쇠 검"})
    sim = chrf_score("무쇠 검", "철 검")
    expected = (W_PLACEHOLDER + W_FORMAT + W_SIMILARITY * sim) / (
        W_PLACEHOLDER + W_FORMAT + W_SIMILARITY
    )
    assert metric(ex, pred).score == pytest.approx(expected)


def test_metric_score_identical_with_pred_name() -> None:
    ex = _example({"a": "Hello {{PH1}}"}, {"a": "안녕하세요 {{PH1}}"})
    metric = make_metric()
    pred = dspy.Prediction(translations={"a": "안녕 {{PH1}}"})
    plain = metric(ex, pred)
    trace = [(None, {"entries": dict(ex.entries)}, pred)]
    named = metric(ex, pred, None, "translate.predict", trace)
    assert named.score == plain.score


def test_metric_translate_feedback_uses_trace_not_final_output() -> None:
    # translate dropped the token; refine later fixed it — the final pred
    # is clean but translate's feedback must still show ITS OWN failure.
    ex = _example({"a": "Hello {{PH1}}"}, {"a": "안녕하세요 {{PH1}}"})
    metric = make_metric()
    final = dspy.Prediction(translations={"a": "안녕하세요 {{PH1}}"})
    raw = dspy.Prediction(translations={"a": "안녕하세요"})
    trace = [(None, {"entries": dict(ex.entries)}, raw)]
    result = metric(ex, final, None, "translate.predict", trace)
    assert result.score == 1.0  # module-level score stays final
    assert "{{PH1}}" in result.feedback  # trace diagnosis shows the drop
    assert "translate call" in result.feedback


def test_metric_refine_feedback_reports_fix_status() -> None:
    ex = _example({"a": "Hello {{PH1}}"}, {"a": "안녕하세요 {{PH1}}"})
    metric = make_metric()
    final = dspy.Prediction(translations={"a": "안녕하세요 {{PH1}}"})
    refine_inputs = {
        "source": "Hello {{PH1}}",
        "bad_translation": "안녕하세요",
        "validation_errors": "placeholder {{PH1}} dropped (1x)",
        "glossary": "",
        "target_lang": "ko_kr",
    }
    fixed = dspy.Prediction(fixed_translation="안녕하세요 {{PH1}}")
    result = metric(ex, final, None, "refine.predict", [(None, refine_inputs, fixed)])
    assert result.score == 1.0
    assert "passes token validation" in result.feedback
