"""Compiled-artifact matrix: (model tier) x (language pair).

Artifacts are JSON files produced offline by engine/tools/optimize.py and
shipped with the app. Naming scheme:
    translate__{tier}-tier__{source}-{target}.json

Two tiers only. Classifying hosted models by capability (small vs medium)
meant curating a prefix table of every provider's lineup — unmaintainable
and wrong the moment a new model ships. Hosted API models share one
"default" artifact; "local" (Ollama) stays separate because detection is
a mechanical prefix and small local models need genuinely different
instructions than hosted frontier models.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .translator import BatchTranslator

logger = logging.getLogger(__name__)

TIERS = ("default", "local")


def resolve_tier(model: str) -> str:
    """Map a LiteLLM model string to an artifact tier.

    "local" for Ollama-served models, "default" for everything hosted.
    """
    return "local" if model.startswith(("ollama_chat/", "ollama/")) else "default"


def artifacts_dir() -> Path:
    """Artifact directory: env override first (bundled builds), then the
    repo-layout default engine/artifacts."""
    env = os.environ.get("MORU_ARTIFACTS_DIR")
    if env:
        return Path(env)
    # __file__ = engine/src/moru_engine/dspy_modules/artifacts.py -> parents[3] = engine/
    return Path(__file__).resolve().parents[3] / "artifacts"


def artifact_path(
    tier: str,
    source_lang: str,
    target_lang: str,
    base_dir: Path | None = None,
) -> Path:
    base = base_dir if base_dir is not None else artifacts_dir()
    return base / f"translate__{tier}-tier__{source_lang}-{target_lang}.json"


def load_translator(
    model: str,
    source_lang: str,
    target_lang: str,
    *,
    max_refine: int = 2,
    base_dir: Path | None = None,
) -> tuple[BatchTranslator, str | None]:
    """Build a BatchTranslator, loading the compiled artifact when present.

    Returns:
        (translator, artifact_id) — artifact_id is the artifact filename
        stem, or None when running with seed instructions (uncompiled).
    """
    translator = BatchTranslator(max_refine=max_refine)
    tier = resolve_tier(model)
    path = artifact_path(tier, source_lang, target_lang, base_dir)
    if path.exists():
        translator.load(str(path))
        logger.info("Loaded compiled artifact: %s", path.name)
        return translator, path.stem
    logger.warning(
        "No compiled artifact for tier=%s pair=%s-%s; using seed instructions",
        tier,
        source_lang,
        target_lang,
    )
    return translator, None
