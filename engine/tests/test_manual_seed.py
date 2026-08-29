"""Manual-seed runs: a work queue for a human, produced with no provider.

Three properties matter and each has a real failure mode:

* The run must complete with **no API key and no model**. If it ever builds an
  LM, hand translation stops working for the user who has configured nothing,
  which is the entire premise of the feature.
* Untranslated entries must be **PENDING and carry no text**, and PENDING must
  never reach output. An untranslated string shipping into a resource pack is
  worse than shipping nothing.
* The LLM-free helper stages must **stay on**. Only glossary curation reaches a
  provider, so disabling TM/glossaries/graph "for symmetry" with a source-text
  run would strip the aids a translator actually leans on for no gain.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moru_engine.pipeline.orchestrator import (
    EntryStatus,
    PipelineConfig,
    TranslationPipeline,
    write_outputs,
)


def _lang_pack(
    root: Path, entries: dict[str, str], target: dict[str, str] | None = None
):
    """A minimal lang tree: en_us source, optional ko_kr target.

    Uses the kubejs layout rather than mods/<name>.jar/: a directory named
    ".jar" makes the scanner try to open it as an archive and skip it.
    """
    base = root / "kubejs" / "assets" / "testmod" / "lang"
    base.mkdir(parents=True, exist_ok=True)
    (base / "en_us.json").write_text(json.dumps(entries), encoding="utf-8")
    if target is not None:
        (base / "ko_kr.json").write_text(json.dumps(target), encoding="utf-8")
    return base


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    root = tmp_path / "pack"
    root.mkdir()
    _lang_pack(
        root,
        {
            "item.testmod.ingot": "Osmium Ingot",
            "item.testmod.gear": "Steel Gear",
            "tooltip.testmod.desc1": "Holds up to %s items.",
        },
    )
    return root


def _config(pack: Path, tmp_path: Path, **kw) -> PipelineConfig:
    return PipelineConfig(
        modpack_path=pack,
        output_dir=tmp_path / "out",
        manual_seed=True,
        # No model, no api_key: the point of the test.
        use_tm=False,
        use_vanilla_glossary=False,
        use_user_glossary=False,
        use_mod_translations=False,
        glossary_store_dir=tmp_path / "glossaries",
        **kw,
    )


# ---- the no-provider promise -------------------------------------------


def test_manual_seed_builds_no_lm_and_no_translator(pack: Path, tmp_path: Path):
    pipeline = TranslationPipeline(_config(pack, tmp_path))
    assert pipeline.lm is None
    assert pipeline.translator is None
    assert pipeline.artifact_id is None


def test_manual_seed_forces_glossary_curation_off(pack: Path, tmp_path: Path):
    """The only genuinely LLM-bound stage, so the only one switched off."""
    config = PipelineConfig(modpack_path=pack, manual_seed=True, extract_glossary=True)
    assert config.extract_glossary is False


def test_manual_seed_keeps_the_llm_free_helper_stages_on(pack: Path):
    """The D2 finding, locked down.

    A source-text run disables six stages because its output IS the source. A
    manual seed must not copy that: TM, both glossaries, the mod-translation
    harvest and the sibling graph are pure local work and are exactly the aids
    a hand translator needs.
    """
    config = PipelineConfig(
        modpack_path=pack,
        manual_seed=True,
        use_tm=True,
        use_translation_graph=True,
        use_vanilla_glossary=True,
        use_user_glossary=True,
        use_mod_translations=True,
        bilingual_names=True,
    )
    assert config.use_tm is True
    assert config.use_translation_graph is True
    assert config.use_vanilla_glossary is True
    assert config.use_user_glossary is True
    assert config.use_mod_translations is True
    # An output-shape choice about the eventual export, orthogonal to who
    # produced the translation.
    assert config.bilingual_names is True


def test_source_text_only_still_disables_everything(pack: Path):
    """The narrower manual_seed branch must not weaken the existing one."""
    config = PipelineConfig(
        modpack_path=pack,
        source_text_only=True,
        use_tm=True,
        use_translation_graph=True,
        use_vanilla_glossary=True,
        use_user_glossary=True,
        use_mod_translations=True,
        extract_glossary=True,
        bilingual_names=True,
    )
    assert not any(
        (
            config.use_tm,
            config.use_translation_graph,
            config.use_vanilla_glossary,
            config.use_user_glossary,
            config.use_mod_translations,
            config.extract_glossary,
            config.bilingual_names,
        )
    )


# ---- what a seed run produces -----------------------------------------


@pytest.mark.asyncio
async def test_seed_leaves_every_translatable_entry_pending(pack: Path, tmp_path: Path):
    result = await TranslationPipeline(_config(pack, tmp_path)).run()

    pending = [e for e in result.entries if e.status is EntryStatus.PENDING]
    assert pending, "a seed run must produce work"
    assert {e.key for e in pending} == {
        "item.testmod.ingot",
        "item.testmod.gear",
        "tooltip.testmod.desc1",
    }
    # No text at all: a blank box for the human, not a fake answer.
    assert all(e.translated_text is None for e in pending)
    assert all(e.source_text for e in pending)


@pytest.mark.asyncio
async def test_seed_writes_no_output_tree(pack: Path, tmp_path: Path):
    """A seed produces a queue, not an artifact."""
    result = await TranslationPipeline(_config(pack, tmp_path)).run()
    assert result.entries
    out = tmp_path / "out"
    assert not out.exists() or not any(out.rglob("*.json"))


@pytest.mark.asyncio
async def test_pending_never_reaches_output_even_if_generation_runs(
    pack: Path, tmp_path: Path
):
    """The guard in write_outputs, exercised directly.

    Export happens later from reviewed entries, so write_outputs WILL be called
    on a result containing PENDING entries. None of them may appear.
    """
    result = await TranslationPipeline(_config(pack, tmp_path)).run()
    generation = await write_outputs(result)

    written = [p for p in generation.all_files if p.suffix == ".json"]
    for path in written:
        if path.name == "pack.mcmeta":
            continue
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert "item.testmod.ingot" not in data
        assert "tooltip.testmod.desc1" not in data


@pytest.mark.asyncio
async def test_a_pack_translation_is_kept_not_re_queued(tmp_path: Path):
    """Entries the pack already translated are not the human's work to do."""
    root = tmp_path / "pack2"
    root.mkdir()
    _lang_pack(
        root,
        {"item.testmod.a": "Alpha", "item.testmod.b": "Beta"},
        target={"item.testmod.a": "알파"},
    )
    result = await TranslationPipeline(_config(root, tmp_path)).run()

    by_key = {e.key: e for e in result.entries}
    assert by_key["item.testmod.a"].status is EntryStatus.SKIPPED
    assert by_key["item.testmod.a"].translated_text == "알파"
    assert by_key["item.testmod.b"].status is EntryStatus.PENDING


# ---- adoption is by provenance, not content ---------------------------


@pytest.mark.asyncio
async def test_human_translation_equal_to_its_source_is_never_re_sent(
    tmp_path: Path,
):
    """The landmine this rule exists to prevent.

    A translator deliberately keeps a proper noun in English. Adopting by
    content would route that through is_untranslated_copy, read it as
    untranslated filler, and hand it back to the model — silently overwriting a
    decision the human made on purpose.
    """
    root = tmp_path / "pack3"
    root.mkdir()
    _lang_pack(root, {"item.testmod.brand": "Mekanism", "item.testmod.x": "Widget"})

    rel = "kubejs/assets/testmod/lang/en_us.json"
    pipeline = TranslationPipeline(
        _config(root, tmp_path),
        # target == source, deliberately.
        human_translations={rel: {"item.testmod.brand": "Mekanism"}},
    )
    result = await pipeline.run()

    by_key = {e.key: e for e in result.entries}
    brand = by_key["item.testmod.brand"]
    assert brand.status is EntryStatus.SKIPPED, (
        "a human's deliberate keep-as-English must be adopted, not re-queued"
    )
    assert brand.translated_text == "Mekanism"
    # Everything else is still the human's to do.
    assert by_key["item.testmod.x"].status is EntryStatus.PENDING


@pytest.mark.asyncio
async def test_adoption_ignores_keys_absent_from_the_scan(tmp_path: Path):
    """A journal entry whose key no longer exists must not invent one."""
    root = tmp_path / "pack4"
    root.mkdir()
    _lang_pack(root, {"item.testmod.a": "Alpha"})

    rel = "kubejs/assets/testmod/lang/en_us.json"
    pipeline = TranslationPipeline(
        _config(root, tmp_path),
        human_translations={rel: {"item.testmod.gone": "사라짐"}},
    )
    result = await pipeline.run()
    assert {e.key for e in result.entries} == {"item.testmod.a"}
