"""DSPy program surface: signatures, modules, LM factory, artifact loader."""

from __future__ import annotations

from .artifacts import artifact_path, load_translator, resolve_tier
from .lm import build_lm, configure_engine
from .signatures import (
    CurateGlossaryTerms,
    JudgeTranslationQuality,
    RefineTranslation,
    TranslateEntries,
)
from .translator import BatchTranslator, GlossaryExtractor

__all__ = [
    "BatchTranslator",
    "CurateGlossaryTerms",
    "GlossaryExtractor",
    "JudgeTranslationQuality",
    "RefineTranslation",
    "TranslateEntries",
    "artifact_path",
    "build_lm",
    "configure_engine",
    "load_translator",
    "resolve_tier",
]
