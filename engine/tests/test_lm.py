"""build_lm parameter selection for provider quirks.

LiteLLM's gpt-5 transformation rejects temperature != 1 for model ids
missing from its bundled map (the 5.6 family), so build_lm must send
temperature=None (never forwarded) for every gpt-5-family id while
leaving other providers' sampling temperature intact.
"""

from moru_engine.dspy_modules.lm import build_lm


def test_gpt56_direct_omits_temperature():
    lm = build_lm("openai/gpt-5.6-luna", temperature=0.3, cache=False)
    assert lm.kwargs["temperature"] is None
    assert lm.kwargs["max_tokens"] == 8192


def test_gpt56_via_openrouter_omits_temperature():
    lm = build_lm("openrouter/openai/gpt-5.6-sol", temperature=0.7, cache=False)
    assert lm.kwargs["temperature"] is None


def test_non_gpt5_models_keep_temperature():
    lm = build_lm("anthropic/claude-sonnet-4-6", temperature=0.3, cache=False)
    assert lm.kwargs["temperature"] == 0.3

    lm = build_lm("openai/gpt-4.1", temperature=0.3, cache=False)
    assert lm.kwargs["temperature"] == 0.3


def test_openrouter_reasoning_effort_maps_to_extra_body():
    """litellm rejects reasoning_effort for openrouter; build_lm must
    translate it to the native extra_body reasoning object."""
    lm = build_lm(
        "openrouter/~deepseek/deepseek-v4-flash-latest",
        reasoning_effort="disable",
        cache=False,
    )
    assert "reasoning_effort" not in lm.kwargs
    assert lm.kwargs["extra_body"]["reasoning"] == {"enabled": False}

    lm = build_lm(
        "openrouter/anthropic/claude-sonnet-5",
        reasoning_effort="low",
        cache=False,
    )
    assert "reasoning_effort" not in lm.kwargs
    assert lm.kwargs["extra_body"]["reasoning"] == {"effort": "low"}


def test_reasoning_effort_untouched_off_openrouter():
    lm = build_lm("openai/gpt-4.1", reasoning_effort="low", cache=False)
    assert lm.kwargs["reasoning_effort"] == "low"
    assert "extra_body" not in lm.kwargs

    # Ollama thinking models still default to disable (litellm think=false)
    lm = build_lm("ollama_chat/qwen3:8b", cache=False)
    assert lm.kwargs["reasoning_effort"] == "disable"
