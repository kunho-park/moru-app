"""Focused unit and integration coverage for pipeline orchestration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import dspy
import moru_engine.dspy_modules.lm as lm_module
import moru_engine.pipeline.orchestrator as orchestrator
import pytest
from moru_engine.models import Glossary, LanguageFilePair, TermRule
from moru_engine.pipeline.orchestrator import (
    EntryResult,
    EntryStatus,
    PipelineConfig,
    PipelineResult,
    TranslationPipeline,
    category_stats,
    looks_like_identifier,
    write_outputs,
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


class FailingTranslator:
    async def acall(self, **kwargs: Any) -> dspy.Prediction:
        entries = kwargs["entries"]
        return dspy.Prediction(
            translations={},
            failed={key: ["provider rejected request"] for key in entries},
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


@pytest.mark.asyncio
async def test_failed_batches_still_reach_full_progress_and_emit_failures(
    tmp_path: Path,
) -> None:
    modpack_path = tmp_path / "modpack"
    source_path = modpack_path / "kubejs/assets/test/lang/en_us.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        json.dumps({"one": "Hello one", "two": "Hello two"}),
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
    events: list[tuple[str, dict[str, object]]] = []
    pipeline = TranslationPipeline(
        PipelineConfig(
            modpack_path=modpack_path,
            output_dir=tmp_path / "out",
            batch_size=2,
            use_tm=False,
            use_mod_translations=False,
            use_user_glossary=False,
            use_vanilla_glossary=False,
        ),
        lm=dspy.utils.DummyLM([]),
        on_event=lambda event, payload: events.append((event, payload)),
    )
    pipeline.translator = FailingTranslator()
    try:
        result = await pipeline.run(scan_result)
    finally:
        pipeline.close()

    failures = [payload for event, payload in events if event == "entry_failed"]
    progress = [
        payload
        for event, payload in events
        if event == "progress" and payload.get("stage") == "translate"
    ]
    assert {str(payload["key"]) for payload in failures} == {"one", "two"}
    assert progress[-1]["done"] == progress[-1]["total"] == 2
    assert result.stats.failed_entries == 2


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


def _seed_lang_modpack(modpack_path: Path) -> EntryResult:
    """One translated lang entry backed by a real source file on disk."""
    lang_dir = modpack_path / "assets" / "somemod" / "lang"
    lang_dir.mkdir(parents=True)
    (lang_dir / "en_us.json").write_text('{"gui.ok": "OK"}', encoding="utf-8")
    return EntryResult(
        key="gui.ok",
        file="assets/somemod/lang/en_us.json",
        source_text="OK",
        translated_text="확인",
        status=EntryStatus.PASSED,
    )


@pytest.mark.asyncio
async def test_pack_description_is_version_slash_attribution(
    tmp_path: Path,
) -> None:
    # "v{version} / §a모루§7로 한국어로 번역됨" — the pack list already shows
    # the pack's name, so the description carries only version + credit.
    modpack_path = tmp_path / "modpack"
    modpack_path.mkdir()
    entry = _seed_lang_modpack(modpack_path)
    (modpack_path / "manifest.json").write_text(
        json.dumps(
            {
                "manifestType": "minecraftModpack",
                "name": "Linggango - [V6.5 IS OUT]",
                "version": "6.5.4hotfix",
                "minecraft": {
                    "version": "1.20.1",
                    "modLoaders": [{"id": "forge-47.3.0", "primary": True}],
                },
            }
        ),
        encoding="utf-8",
    )
    result = PipelineResult(
        config=PipelineConfig(
            modpack_path=modpack_path, output_dir=tmp_path / "out"
        ),
        entries=[entry],
    )

    await write_outputs(result)

    mcmeta = json.loads(
        (tmp_path / "out" / "resourcepack" / "pack.mcmeta").read_text(
            encoding="utf-8"
        )
    )
    assert (
        mcmeta["pack"]["description"]
        == "v6.5.4hotfix / §a모루§7로 한국어로 번역됨 — §amoru.gg"
    )


@pytest.mark.asyncio
async def test_pack_description_without_version_keeps_attribution_only(
    tmp_path: Path,
) -> None:
    modpack_path = tmp_path / "modpack"
    modpack_path.mkdir()
    entry = _seed_lang_modpack(modpack_path)
    result = PipelineResult(
        config=PipelineConfig(
            modpack_path=modpack_path, output_dir=tmp_path / "out"
        ),
        entries=[entry],
    )

    await write_outputs(result)

    mcmeta = json.loads(
        (tmp_path / "out" / "resourcepack" / "pack.mcmeta").read_text(
            encoding="utf-8"
        )
    )
    assert (
        mcmeta["pack"]["description"] == "§a모루§7로 한국어로 번역됨 — §amoru.gg"
    )



class FullRecordingTranslator:
    """Record (context, glossary, entries) for every LLM batch, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def acall(
        self,
        *,
        source_lang: str,
        target_lang: str,
        context: str,
        glossary: str,
        entries: dict[str, str],
    ) -> dspy.Prediction:
        self.calls.append((context, glossary, dict(entries)))
        return dspy.Prediction(
            translations={key: f"KO {text}" for key, text in entries.items()},
            failed={},
        )


def _graph_scan_fixture(tmp_path: Path) -> tuple[Path, ScanResult]:
    """A lang file defining a name + a second file mentioning it."""
    modpack_path = tmp_path / "modpack"
    lang_path = modpack_path / "kubejs/assets/testmod/lang/en_us.json"
    lang_path.parent.mkdir(parents=True)
    lang_path.write_text(
        json.dumps({"item.testmod.void_orb": "Void Orb"}),
        encoding="utf-8",
    )
    quest_path = modpack_path / "kubejs/assets/questmod/lang/en_us.json"
    quest_path.parent.mkdir(parents=True)
    quest_path.write_text(
        json.dumps({"gui.questmod.hint": "Collect the Void Orb"}),
        encoding="utf-8",
    )
    scan_result = ScanResult(
        modpack_path=modpack_path,
        source_only_files=[
            LanguageFilePair(source_path=lang_path),
            LanguageFilePair(source_path=quest_path),
        ],
        translation_files=[
            TranslationFile(input_path=str(lang_path), file_type="kubejs"),
            TranslationFile(input_path=str(quest_path), file_type="kubejs"),
        ],
    )
    return modpack_path, scan_result


def _graph_config(tmp_path: Path, modpack_path: Path, **overrides: Any) -> PipelineConfig:
    return PipelineConfig(
        modpack_path=modpack_path,
        output_dir=tmp_path / "out",
        use_tm=False,
        use_user_glossary=False,
        use_vanilla_glossary=False,
        **overrides,
    )


@pytest.mark.asyncio
async def test_graph_schedules_name_wave_first_and_merges_bindings(
    tmp_path: Path,
) -> None:
    """Names translate first; their settled translations reach later
    batches as glossary bindings and land on result.glossary."""
    modpack_path, scan_result = _graph_scan_fixture(tmp_path)
    translator = FullRecordingTranslator()
    pipeline = TranslationPipeline(
        _graph_config(tmp_path, modpack_path), lm=dspy.utils.DummyLM([])
    )
    pipeline.translator = translator
    try:
        result = await pipeline.run(scan_result)
    finally:
        pipeline.close()

    orb_index = next(
        i
        for i, (_, _, entries) in enumerate(translator.calls)
        if "item.testmod.void_orb" in entries
    )
    hint_index = next(
        i
        for i, (_, _, entries) in enumerate(translator.calls)
        if "gui.questmod.hint" in entries
    )
    # Wave barrier: the name batch strictly precedes the mention batch.
    assert orb_index < hint_index

    # The mention batch saw the wave-1 translation as a glossary binding.
    _, hint_glossary, _ = translator.calls[hint_index]
    assert "Void Orb" in hint_glossary
    assert "KO Void Orb" in hint_glossary

    # And the binding is part of the run glossary for post-run paths.
    assert result.glossary is not None
    rule = next(
        r for r in result.glossary.term_rules if r.aliases == ["Void Orb"]
    )
    assert rule.term_ko == "KO Void Orb"
    assert rule.category == "item"

    entries = {e.key: e for e in result.entries}
    assert entries["item.testmod.void_orb"].status is EntryStatus.PASSED
    # The stub ignored the binding, so the merged validator surfaces the
    # inconsistency as a WARNING - the consistency audit works with zero
    # validator changes.
    hint = entries["gui.questmod.hint"]
    assert hint.status is EntryStatus.WARNING
    assert any("KO Void Orb" in error for error in hint.errors)


@pytest.mark.asyncio
async def test_graph_disabled_keeps_single_wave_without_injection(
    tmp_path: Path,
) -> None:
    modpack_path, scan_result = _graph_scan_fixture(tmp_path)
    translator = FullRecordingTranslator()
    pipeline = TranslationPipeline(
        _graph_config(tmp_path, modpack_path, use_translation_graph=False),
        lm=dspy.utils.DummyLM([]),
    )
    pipeline.translator = translator
    try:
        result = await pipeline.run(scan_result)
    finally:
        pipeline.close()

    # No binding ever reaches a prompt glossary...
    assert all("KO Void Orb" not in glossary for _, glossary, _ in translator.calls)
    # ...and the run glossary carries no graph-derived rule.
    assert result.glossary is not None
    assert all(r.aliases != ["Void Orb"] for r in result.glossary.term_rules)
    statuses = {e.key: e.status for e in result.entries}
    assert statuses["gui.questmod.hint"] is EntryStatus.PASSED


class CancelOnKeyTranslator:
    """Translate normally until a batch contains the poisoned key."""

    def __init__(self, cancel_key: str) -> None:
        self.cancel_key = cancel_key

    async def acall(
        self,
        *,
        source_lang: str,
        target_lang: str,
        context: str,
        glossary: str,
        entries: dict[str, str],
    ) -> dspy.Prediction:
        if self.cancel_key in entries:
            raise asyncio.CancelledError("simulated mid-flight cancellation")
        return dspy.Prediction(
            translations={key: f"KO {text}" for key, text in entries.items()},
            failed={},
        )


@pytest.mark.asyncio
async def test_wave2_cancellation_preserves_wave1_entries(
    tmp_path: Path,
) -> None:
    modpack_path, scan_result = _graph_scan_fixture(tmp_path)
    pipeline = TranslationPipeline(
        _graph_config(tmp_path, modpack_path), lm=dspy.utils.DummyLM([])
    )
    pipeline.translator = CancelOnKeyTranslator("gui.questmod.hint")
    try:
        result = await pipeline.run(scan_result)
    finally:
        pipeline.close()

    orb = next(e for e in result.entries if e.key == "item.testmod.void_orb")
    assert orb.status is EntryStatus.PASSED
    assert orb.translated_text == "KO Void Orb"
    assert all(e.key != "gui.questmod.hint" for e in result.entries)


@pytest.mark.asyncio
async def test_wave_batches_receive_sibling_context(tmp_path: Path) -> None:
    """A wave-2 batch sees its wave-1 sibling's settled translation."""
    modpack_path = tmp_path / "modpack"
    lang_path = modpack_path / "kubejs/assets/testmod/lang/en_us.json"
    lang_path.parent.mkdir(parents=True)
    lang_path.write_text(
        json.dumps(
            {
                "item.testmod.void_orb": "Void Orb",
                "item.testmod.void_orb.tooltip": "It hums quietly.",
            }
        ),
        encoding="utf-8",
    )
    scan_result = ScanResult(
        modpack_path=modpack_path,
        source_only_files=[LanguageFilePair(source_path=lang_path)],
        translation_files=[
            TranslationFile(input_path=str(lang_path), file_type="kubejs")
        ],
    )
    translator = FullRecordingTranslator()
    pipeline = TranslationPipeline(
        _graph_config(tmp_path, modpack_path), lm=dspy.utils.DummyLM([])
    )
    pipeline.translator = translator
    try:
        await pipeline.run(scan_result)
    finally:
        pipeline.close()

    tooltip_context = next(
        context
        for context, _, entries in translator.calls
        if "item.testmod.void_orb.tooltip" in entries
    )
    assert "Already-translated related entries" in tooltip_context
    assert (
        '- item.testmod.void_orb: "Void Orb" => "KO Void Orb"'
        in tooltip_context
    )
    # The name batch itself had no settled siblings yet.
    orb_context = next(
        context
        for context, _, entries in translator.calls
        if set(entries) == {"item.testmod.void_orb"}
    )
    assert "Already-translated" not in orb_context


def _sibling_result(config: PipelineConfig) -> PipelineResult:
    rel = "config/ftbquests/quests/chapters/intro.snbt"
    return PipelineResult(
        config=config,
        glossary=Glossary(),
        entries=[
            EntryResult(
                key="quests[0].title",
                file=rel,
                source_text="First Steps",
                translated_text="첫 걸음",
                status=EntryStatus.PASSED,
            ),
            EntryResult(
                key="quests[0].description[0]",
                file=rel,
                source_text="Collect ten stones.",
                status=EntryStatus.FAILED,
                errors=["boom"],
            ),
            EntryResult(
                key="quests[0].description[1]",
                file=rel,
                source_text="Then craft a pickaxe.",
                status=EntryStatus.FAILED,
                errors=["boom"],
            ),
        ],
    )


@pytest.mark.asyncio
async def test_retranslate_entry_gets_sibling_context_via_from_entries(
    tmp_path: Path,
) -> None:
    """A fresh pipeline (server retranslate path) rebuilds the graph from
    result entries and records new translations back into it."""
    config = PipelineConfig(
        modpack_path=tmp_path,
        use_tm=False,
        use_user_glossary=False,
        use_vanilla_glossary=False,
    )
    result = _sibling_result(config)
    translator = FullRecordingTranslator()
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    pipeline.translator = translator
    try:
        entry = await pipeline.retranslate_entry(
            result, "quests[0].description[0]"
        )
        assert entry.status is EntryStatus.MODIFIED
        context = translator.calls[-1][0]
        assert context.startswith("retranslate; file: ")
        assert '- quests[0].title: "First Steps" => "첫 걸음"' in context

        # The success was recorded: the next sibling retranslate sees it.
        await pipeline.retranslate_entry(result, "quests[0].description[1]")
        context = translator.calls[-1][0]
        assert (
            '- quests[0].description[0]: "Collect ten stones." '
            '=> "KO Collect ten stones."' in context
        )
    finally:
        pipeline.close()


@pytest.mark.asyncio
async def test_retranslate_entry_without_graph_keeps_plain_context(
    tmp_path: Path,
) -> None:
    config = PipelineConfig(
        modpack_path=tmp_path,
        use_tm=False,
        use_user_glossary=False,
        use_vanilla_glossary=False,
        use_translation_graph=False,
    )
    result = _sibling_result(config)
    translator = FullRecordingTranslator()
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    pipeline.translator = translator
    try:
        await pipeline.retranslate_entry(result, "quests[0].description[0]")
    finally:
        pipeline.close()
    assert translator.calls[-1][0] == (
        "retranslate; file: config/ftbquests/quests/chapters/intro.snbt"
    )


@pytest.mark.asyncio
async def test_retry_failed_gets_sibling_context(tmp_path: Path) -> None:
    config = PipelineConfig(
        modpack_path=tmp_path,
        use_tm=False,
        use_user_glossary=False,
        use_vanilla_glossary=False,
    )
    result = _sibling_result(config)
    translator = FullRecordingTranslator()
    pipeline = TranslationPipeline(config, lm=dspy.utils.DummyLM([]))
    pipeline.translator = translator
    try:
        await pipeline.retry_failed(result)
    finally:
        pipeline.close()

    context, _, entries = translator.calls[-1]
    assert set(entries) == {
        "quests[0].description[0]",
        "quests[0].description[1]",
    }
    assert context.startswith("retry; file: ")
    assert '- quests[0].title: "First Steps" => "첫 걸음"' in context
    # The failed batch keys themselves are excluded from the block.
    assert "Collect ten stones" not in context
    statuses = {e.key: e.status for e in result.entries}
    assert statuses["quests[0].description[0]"] is EntryStatus.MODIFIED
    assert statuses["quests[0].description[1]"] is EntryStatus.MODIFIED