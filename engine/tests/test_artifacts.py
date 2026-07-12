"""Artifact matrix: tier resolution and compiled-program roundtrip."""

from __future__ import annotations

from pathlib import Path

from moru_engine.dspy_modules import (
    BatchTranslator,
    artifact_path,
    load_translator,
    resolve_tier,
)


def test_resolve_tier_local_vs_hosted() -> None:
    assert resolve_tier("ollama_chat/qwen3:8b") == "local"
    assert resolve_tier("ollama/llama3") == "local"
    # OpenAI-compatible local servers (LM Studio, llama.cpp) via LiteLLM.
    assert resolve_tier("hosted_vllm/qwen2.5-7b-instruct") == "local"
    assert resolve_tier("openai/gpt-4o-mini") == "default"
    assert resolve_tier("anthropic/claude-sonnet-4-5") == "default"
    assert resolve_tier("openrouter/anthropic/claude-sonnet-4.5") == "default"
    # Never-seen hosted models need no classification table.
    assert resolve_tier("acme/unknown-model-9000") == "default"


def test_artifact_naming() -> None:
    path = artifact_path("default", "en_us", "ko_kr", Path("/x"))
    assert path.name == "translate__default-tier__en_us-ko_kr.json"


def test_load_translator_without_artifact_uses_seed(tmp_path: Path) -> None:
    translator, artifact_id = load_translator(
        "openai/gpt-4o-mini", "en_us", "ko_kr", base_dir=tmp_path
    )
    assert artifact_id is None
    assert isinstance(translator, BatchTranslator)


def test_artifact_save_load_roundtrip(tmp_path: Path) -> None:
    program = BatchTranslator()
    evolved = "EVOLVED-INSTRUCTION: preserve every {{PHn}} token."
    program.translate.signature = program.translate.signature.with_instructions(
        evolved
    )
    out = artifact_path("default", "en_us", "ko_kr", tmp_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    program.save(str(out))

    loaded, artifact_id = load_translator(
        "openai/gpt-4o-mini", "en_us", "ko_kr", base_dir=tmp_path
    )
    assert artifact_id == "translate__default-tier__en_us-ko_kr"
    assert loaded.translate.signature.instructions == evolved
