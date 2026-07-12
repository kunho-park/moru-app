"""Focused unit and integration coverage for pipeline orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import dspy
import pytest

import moru_engine.dspy_modules.lm as lm_module
import moru_engine.pipeline.orchestrator as orchestrator
from moru_engine.models import Glossary, LanguageFilePair, TermRule
from moru_engine.pipeline.orchestrator import (
    EntryResult,
    EntryStatus,
    PipelineConfig,
    PipelineResult,
    TranslationPipeline,
    category_stats,
    looks_like_identifier,
)
from moru_engine.scanner import ScanResult, TranslationFile
from moru_engine.server.jobs import JobManager, JobRecord, JobStatus, JobType


@pytest.mark.parametrize(
    "text",
    [
        "booklet.actuallyadditions.chapter.crystals.text.2",
        "minecraft:diamond",
        "1.2.3",
        "config/foo.json",
        "item.modid.some_item",
    ],
)
def test_looks_like_identifier_accepts_untranslatable_references(text: str) -> None:
    assert looks_like_identifier(text)


@pytest.mark.parametrize(
    "text",
    [
        "e.g.",
        "Done.",
        "Hello world",
        "A cool item.",
        "",
        "{player}",
        "안녕하세요",
        "word",
    ],
)
def test_looks_like_identifier_rejects_translatable_text(text: str) -> None:
    assert not looks_like_identifier(text)


class RecordingTranslator:
    """Translate normal entries while recording every LLM-bound batch."""

    def __init__(self) -> None:
        self.batches: list[dict[str, str]] = []

    async def acall(
        self,
        *,
        source_lang: str,
        target_lang: str,
        context: str,
        glossary: str,
        entries: dict[str, str],
    ) -> dspy.Prediction:
        self.batches.append(dict(entries))
        return dspy.Prediction(
            translations={key: f"KO {text}" for key, text in entries.items()},
            failed={},
        )


@pytest.mark.asyncio
async def test_identifier_source_is_skipped_before_the_llm(
    tmp_path: Path,
) -> None:
    modpack_path = tmp_path / "modpack"
    source_path = modpack_path / "kubejs/assets/test/lang/en_us.json"
    source_path.parent.mkdir(parents=True)
    identifier = "booklet.actuallyadditions.chapter.crystals.text.2"
    source_path.write_text(
        json.dumps({"identifier": identifier, "normal": "Hello world"}),
        encoding="utf-8",
    )
    pair = LanguageFilePair(source_path=source_path)
    scan_result = ScanResult(
        modpack_path=modpack_path,
        source_only_files=[pair],
        translation_files=[
            TranslationFile(input_path=str(source_path), file_type="kubejs")
        ],
    )
    config = PipelineConfig(
        modpack_path=modpack_path,
        output_dir=tmp_path / "out",
        use_tm=False,
        use_user_glossary=False,
        use_vanilla_glossary=False,
    )
    translator = RecordingTranslator()
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    pipeline.translator = translator
    try:
        result = await pipeline.run(scan_result)
    finally:
        pipeline.close()

    entry = next(item for item in result.entries if item.key == "identifier")
    assert entry.status is EntryStatus.SKIPPED
    assert entry.translated_text == identifier
    assert translator.batches == [{"normal": "Hello world"}]
    assert result.stats.categories == {"scripts": 2}


class GlossaryRecordingTranslator:
    """Record the (glossary, entries) pair every LLM batch received."""

    def __init__(self) -> None:
        self.batches: list[tuple[str, dict[str, str]]] = []

    async def acall(
        self,
        *,
        source_lang: str,
        target_lang: str,
        context: str,
        glossary: str,
        entries: dict[str, str],
    ) -> dspy.Prediction:
        self.batches.append((glossary, dict(entries)))
        return dspy.Prediction(
            translations={key: f"KO {text}" for key, text in entries.items()},
            failed={},
        )


@pytest.mark.asyncio
async def test_mod_translations_feed_glossary_and_copies_retranslate(
    tmp_path: Path,
) -> None:
    """Paired mod lang files seed the glossary; en_us copies re-translate.

    The ko_kr file ships one real translation (reused verbatim AND
    harvested as a term rule for the rest of the pack) and one untouched
    English copy (which must reach the LLM instead of being skipped).
    """
    modpack_path = tmp_path / "modpack"
    lang_dir = modpack_path / "kubejs/assets/farm/lang"
    lang_dir.mkdir(parents=True)
    (lang_dir / "en_us.json").write_text(
        json.dumps(
            {
                "item.farm.copper_hoe": "Copper Hoe",
                "item.farm.copper_axe": "Copper Axe",
                "gui.farm.greeting": "Sharpen your Copper Hoe",
            }
        ),
        encoding="utf-8",
    )
    (lang_dir / "ko_kr.json").write_text(
        json.dumps(
            {
                "item.farm.copper_hoe": "구리 괭이",
                "item.farm.copper_axe": "Copper Axe",
            }
        ),
        encoding="utf-8",
    )
    pair = LanguageFilePair(
        source_path=lang_dir / "en_us.json",
        target_path=lang_dir / "ko_kr.json",
    )
    scan_result = ScanResult(
        modpack_path=modpack_path,
        paired_files=[pair],
        translation_files=[
            TranslationFile(
                input_path=str(lang_dir / "en_us.json"), file_type="kubejs"
            )
        ],
    )
    config = PipelineConfig(
        modpack_path=modpack_path,
        output_dir=tmp_path / "out",
        use_tm=False,
        use_user_glossary=False,
        use_vanilla_glossary=False,
    )
    translator = GlossaryRecordingTranslator()
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    pipeline.translator = translator
    try:
        result = await pipeline.run(scan_result)
    finally:
        pipeline.close()

    # Harvested rule captured on the result for retry/retranslate reuse.
    assert result.glossary is not None
    rule = next(
        r for r in result.glossary.term_rules if r.aliases == ["Copper Hoe"]
    )
    assert rule.term_ko == "구리 괭이"

    # Real existing translation reused verbatim.
    hoe = next(e for e in result.entries if e.key == "item.farm.copper_hoe")
    assert hoe.status is EntryStatus.SKIPPED
    assert hoe.translated_text == "구리 괭이"

    # The en_us copy is NOT treated as an existing translation.
    axe = next(e for e in result.entries if e.key == "item.farm.copper_axe")
    assert axe.status is not EntryStatus.SKIPPED
    assert axe.translated_text == "KO Copper Axe"

    # Batches whose text mentions the term get the harvested rule in the
    # prompt glossary.
    greeting_glossaries = [
        glossary
        for glossary, entries in translator.batches
        if "gui.farm.greeting" in entries
    ]
    assert greeting_glossaries
    assert all("구리 괭이" in text for text in greeting_glossaries)


@pytest.mark.asyncio
async def test_retranslate_entry_reuses_stored_run_glossary(
    tmp_path: Path,
) -> None:
    """retranslate_entry must see the run's own glossary, not a rebuild.

    Harvested mod terms are run-scoped: with the user store disabled, the
    only way the term below can reach the prompt is via result.glossary.
    """
    config = PipelineConfig(
        modpack_path=tmp_path,
        use_tm=False,
        use_user_glossary=False,
        use_vanilla_glossary=False,
    )
    result = PipelineResult(
        config=config,
        glossary=Glossary(
            term_rules=[
                TermRule(
                    term_ko="구리 괭이",
                    preferred_style="용어 고정",
                    aliases=["Copper Hoe"],
                )
            ]
        ),
        entries=[
            EntryResult(
                key="gui.farm.greeting",
                file="kubejs/assets/farm/lang/en_us.json",
                source_text="Sharpen your Copper Hoe",
                status=EntryStatus.FAILED,
                errors=["boom"],
            )
        ],
    )
    translator = GlossaryRecordingTranslator()
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    pipeline.translator = translator
    try:
        entry = await pipeline.retranslate_entry(result, "gui.farm.greeting")
    finally:
        pipeline.close()

    assert entry.status is EntryStatus.MODIFIED
    assert entry.translated_text == "KO Sharpen your Copper Hoe"
    assert any("구리 괭이" in glossary for glossary, _ in translator.batches)


def test_category_stats_bucket_refresh_and_upload_payload(
    tmp_path: Path,
) -> None:
    modpack_path = tmp_path / "modpack"
    config = PipelineConfig(modpack_path=modpack_path)
    assert category_stats(PipelineResult(config=config)) == {}

    mappings = [
        ("ftbquests", "quests"),
        ("the_vault_quest", "quests"),
        ("patchouli", "guidebook"),
        ("kubejs", "scripts"),
        ("mod", "lang"),
        ("resources", "lang"),
        ("resourcepacks", "lang"),
        ("datapacks", "lang"),
        ("config", "json"),
        ("custom_format", "custom_format"),
    ]
    translation_files: list[TranslationFile] = []
    entries: list[EntryResult] = []
    for index, (file_type, _category) in enumerate(mappings):
        relative = f"sources/file-{index}.json"
        translation_files.append(
            TranslationFile(
                input_path=str(modpack_path / relative),
                file_type=file_type,
            )
        )
        entries.append(
            EntryResult(
                key=f"entry-{index}",
                file=relative,
                source_text="Source",
                translated_text="번역",
                status=EntryStatus.PASSED,
            )
        )

    failed_entry = EntryResult(
        key="failed",
        file="sources/file-0.json",
        source_text="Broken",
        status=EntryStatus.FAILED,
    )
    entries.extend(
        [
            failed_entry,
            EntryResult(
                key="unmapped",
                file="sources/not-scanned.json",
                source_text="Source",
                translated_text="번역",
                status=EntryStatus.PASSED,
            ),
        ]
    )
    result = PipelineResult(
        config=config,
        scan_result=ScanResult(
            modpack_path=modpack_path,
            translation_files=translation_files,
        ),
        entries=entries,
    )
    expected = {
        "quests": 2,
        "guidebook": 1,
        "scripts": 1,
        "lang": 4,
        "json": 1,
        "custom_format": 1,
    }

    assert category_stats(result) == expected
    TranslationPipeline._refresh_stats(result)
    assert result.stats.categories == expected

    failed_entry.status = EntryStatus.MODIFIED
    failed_entry.translated_text = "수정"
    TranslationPipeline._refresh_stats(result)
    assert result.stats.categories == {**expected, "quests": 3}
    assert _pack_payload(result)["stats"]["categories"] == {
        **expected,
        "quests": 3,
    }


def _pack_payload(result: PipelineResult) -> dict[str, Any]:
    source = JobRecord(
        id="translate",
        type=JobType.TRANSLATE,
        params={},
        status=JobStatus.DONE,
        result=result,
    )
    upload = JobRecord(
        id="upload",
        type=JobType.UPLOAD,
        params={"modpack_name": "Test Pack"},
    )
    return JobManager._pack_payload(upload, source, {})


def test_pack_payload_omits_empty_categories(tmp_path: Path) -> None:
    result = PipelineResult(config=PipelineConfig(modpack_path=tmp_path))
    assert "categories" not in _pack_payload(result)["stats"]


def test_pipeline_config_passes_reasoning_effort_to_lm_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}
    built_lm = object()

    def fake_build_lm(model: str, **kwargs: object) -> object:
        seen["model"] = model
        seen["kwargs"] = kwargs
        return built_lm

    monkeypatch.setattr(orchestrator, "build_lm", fake_build_lm)
    monkeypatch.setattr(
        orchestrator,
        "load_translator",
        lambda *args, **kwargs: (object(), None),
    )
    config = PipelineConfig(
        modpack_path=tmp_path,
        model="ollama_chat/qwen3:8b",
        reasoning_effort="high",
        use_tm=False,
    )
    pipeline = TranslationPipeline(config)
    try:
        assert pipeline.lm is built_lm
    finally:
        pipeline.close()

    assert config.reasoning_effort == "high"
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["reasoning_effort"] == "high"


def test_build_lm_forwards_explicit_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    built_lm = object()

    def fake_lm(model: str, **kwargs: object) -> object:
        seen["model"] = model
        seen["kwargs"] = kwargs
        return built_lm

    monkeypatch.setattr(lm_module.dspy, "LM", fake_lm)
    result = lm_module.build_lm(
        "ollama_chat/qwen3:8b",
        reasoning_effort="high",
    )

    assert result is built_lm
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["reasoning_effort"] == "high"
