"""BatchTranslator behavior and LM usage accounting with deterministic fakes."""

from __future__ import annotations

from types import SimpleNamespace

import dspy
from dspy.utils import DummyLM

from moru_engine.dspy_modules import BatchTranslator
from moru_engine.dspy_modules.lm import token_usage
from moru_engine.dspy_modules.translator import check_protected

ENTRIES = {"a.b": "Hello {{PH1}} world", "c.d": "Iron Sword"}
GOOD = {"a.b": "안녕 {{PH1}} 세계", "c.d": "철 검"}


def _run(lm: DummyLM, max_refine: int = 2) -> dspy.Prediction:
    with dspy.context(lm=lm, adapter=dspy.JSONAdapter()):
        return BatchTranslator(max_refine=max_refine)(
            source_lang="en_us",
            target_lang="ko_kr",
            context="test",
            glossary="",
            entries=ENTRIES,
        )


def test_check_protected() -> None:
    assert check_protected("x {{PH1}}", "y {{PH1}}") == []
    assert check_protected("x {{PH1}}", "y")  # dropped
    assert check_protected("x", "y {{PH1}}")  # invented
    assert check_protected("x", None)
    assert check_protected("x", "  ")


def test_happy_path_no_refine() -> None:
    lm = DummyLM([{"translations": GOOD}], adapter=dspy.JSONAdapter())
    pred = _run(lm)
    assert pred.translations == GOOD
    assert pred.failed == {}


def test_refine_fixes_dropped_placeholder() -> None:
    lm = DummyLM(
        [
            {"translations": {**GOOD, "a.b": "안녕 세계"}},
            {"fixed_translation": "안녕 {{PH1}} 세계"},
        ],
        adapter=dspy.JSONAdapter(),
    )
    pred = _run(lm)
    assert pred.translations == GOOD
    assert pred.failed == {}


def test_refine_exhaustion_surfaces_failure() -> None:
    lm = DummyLM(
        [
            {"translations": {**GOOD, "a.b": "안녕 세계"}},
            {"fixed_translation": "여전히 깨짐"},
            {"fixed_translation": "여전히 깨짐 2"},
        ],
        adapter=dspy.JSONAdapter(),
    )
    pred = _run(lm, max_refine=2)
    assert "a.b" in pred.failed
    assert any("{{PH1}}" in e for e in pred.failed["a.b"])
    # the good key is untouched
    assert pred.translations["c.d"] == GOOD["c.d"]


def test_hallucinated_keys_dropped_and_missing_failed() -> None:
    lm = DummyLM(
        [
            {"translations": {"c.d": "철 검", "made.up": "환각"}},
            {"fixed_translation": "안녕 {{PH1}} 세계"},
        ],
        adapter=dspy.JSONAdapter(),
    )
    pred = _run(lm)
    assert "made.up" not in pred.translations
    # missing a.b was refined back in
    assert pred.translations["a.b"] == "안녕 {{PH1}} 세계"
    assert pred.failed == {}


async def test_acall_async_path_matches_forward() -> None:
    lm = DummyLM(
        [
            {"translations": {**GOOD, "a.b": "안녕 세계"}},
            {"fixed_translation": "안녕 {{PH1}} 세계"},
        ],
        adapter=dspy.JSONAdapter(),
    )
    with dspy.context(lm=lm, adapter=dspy.JSONAdapter()):
        pred = await BatchTranslator(max_refine=2).acall(
            source_lang="en_us",
            target_lang="ko_kr",
            context="test",
            glossary="",
            entries=ENTRIES,
        )
    assert pred.translations == GOOD
    assert pred.failed == {}


def test_strip_invented_formatting() -> None:
    from moru_engine.dspy_modules.translator import strip_invented_formatting

    # invented RESET (not in source) is stripped
    assert (
        strip_invented_formatting("{{COLOR1}}Enchanted", "{{COLOR1}}마법 부여됨{{RESET2}}")
        == "{{COLOR1}}마법 부여됨"
    )
    # duplicated known COLOR keeps the first occurrence only
    assert (
        strip_invented_formatting("{{COLOR1}}x", "{{COLOR1}}가{{COLOR1}}나")
        == "{{COLOR1}}가나"
    )
    # invented ARG is NOT stripped (must fail loudly downstream)
    out = strip_invented_formatting("plain", "값 {{ARG9}}")
    assert "{{ARG9}}" in out
    assert check_protected("plain", out)
    # exact output untouched
    assert (
        strip_invented_formatting("{{COLOR1}}a{{RESET2}}", "{{COLOR1}}가{{RESET2}}")
        == "{{COLOR1}}가{{RESET2}}"
    )


def test_token_usage_sums_cached_tokens_across_provider_shapes() -> None:
    history = [
        # OpenAI/OpenRouter style: dict usage, nested details dict
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "prompt_tokens_details": {"cached_tokens": 60},
            }
        },
        # Anthropic style: pydantic-like object, attribute access only
        {
            "usage": SimpleNamespace(
                prompt_tokens=200,
                completion_tokens=50,
                cache_read_input_tokens=150,
            )
        },
        # object usage with object details; zero top-level fallback ignored
        {
            "usage": SimpleNamespace(
                prompt_tokens=80,
                completion_tokens=20,
                prompt_tokens_details=SimpleNamespace(cached_tokens=40),
                cache_read_input_tokens=0,
            )
        },
        # no cache info at all
        {"usage": {"prompt_tokens": 30, "completion_tokens": 10}},
        # provider over-reports the cache: clamped to that call's prompt
        {
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 999},
            }
        },
    ]
    usage = token_usage(SimpleNamespace(history=history))
    assert usage["prompt_tokens"] == 430
    assert usage["completion_tokens"] == 125
    assert usage["total_tokens"] == 555
    assert usage["cached_tokens"] == 60 + 150 + 40 + 0 + 20
    assert usage["cached_tokens"] <= usage["prompt_tokens"]


def test_token_usage_empty_history() -> None:
    usage = token_usage(SimpleNamespace(history=[]))
    assert usage == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }
