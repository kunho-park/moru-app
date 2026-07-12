"""DSPy translation modules.

BatchTranslator is the unit GEPA compiles: one Predict for batch
translation plus one Predict for refining entries that fail the
deterministic in-flight checks. Placeholder protect/restore happens
OUTSIDE this module (pipeline code) — the module only sees typed tokens
({{COLOR}}/{{RESET}}/{{ARG}}/{{VAR}}/{{TAG}}/{{BR}}, numbered only when
one entry mixes distinct literals of a kind) and must preserve every one.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING

import dspy

from ..placeholder import TOKEN_RE
from .signatures import CurateGlossaryTerms, RefineTranslation, TranslateEntries

if TYPE_CHECKING:
    from ..models import TermRule

logger = logging.getLogger(__name__)


def check_protected(source: str, translated: str | None) -> list[str]:
    """Deterministic zero-cost checks on protected (tokenized) text.

    Mirrors the restore() contract of PlaceholderProtector: every source
    token must appear in the translation (order-free, count-exact) and no
    unknown token may be introduced.

    Returns:
        Error strings; empty list means the entry passes.
    """
    if translated is None:
        return ["missing translation for this key"]
    if not translated.strip():
        return ["translation is empty"]

    errors: list[str] = []
    src_tokens = Counter(TOKEN_RE.findall(source))
    out_tokens = Counter(TOKEN_RE.findall(translated))
    missing = src_tokens - out_tokens
    extra = out_tokens - src_tokens
    for token, count in sorted(missing.items()):
        errors.append(
            f"placeholder {token} dropped ({count}x); it must appear exactly as in the source"
        )
    for token, count in sorted(extra.items()):
        errors.append(
            f"placeholder {token} added or duplicated ({count}x); do not invent tokens"
        )
    return errors


# Formatting-only kinds that may be safely stripped when the model INVENTS
# them; deterministic repair, no retranslation needed.
_STRIPPABLE_KINDS = ("RESET", "COLOR")


def strip_invented_formatting(source: str, translated: str) -> str:
    """Remove RESET/COLOR tokens the model added beyond the source counts.

    Models that understand color-span semantics like to "helpfully" close
    spans the source leaves open. Removing the surplus restores the exact
    source token multiset, so the sacred roundtrip invariant is preserved
    — this repairs ONLY provably-safe surplus formatting tokens. Missing
    tokens and invented ARG/VAR/TAG/BR tokens still fail loudly.
    """
    src_tokens = Counter(TOKEN_RE.findall(source))
    surplus = Counter(TOKEN_RE.findall(translated)) - src_tokens
    result = translated
    for token, count in surplus.items():
        kind = token[2:].rstrip("}0123456789")
        if kind not in _STRIPPABLE_KINDS:
            continue
        if src_tokens[token]:
            # duplicated known token: keep the first occurrence
            head, _, tail = result.partition(token)
            result = head + token + tail.replace(token, "", count)
        else:
            result = result.replace(token, "", count)
        logger.debug("Stripped invented formatting token %s (%dx)", token, count)
    return result


class BatchTranslator(dspy.Module):
    """Translate a batch of protected entries, refining only failures.

    forward() is synchronous (GEPA compiles sync programs); the pipeline
    uses acall()/aforward(). Both share the deterministic bookkeeping
    below so their behavior cannot drift.
    """

    def __init__(self, max_refine: int = 2) -> None:
        super().__init__()
        self.translate = dspy.Predict(TranslateEntries)
        self.refine = dspy.Predict(RefineTranslation)
        self.max_refine = max_refine

    @staticmethod
    def _initial_state(
        entries: dict[str, str], pred: dspy.Prediction
    ) -> tuple[dict[str, str], dict[str, list[str]]]:
        """Drop hallucinated keys and collect deterministic failures."""
        translations = {
            k: strip_invented_formatting(entries[k], v)
            for k, v in dict(pred.translations or {}).items()
            if k in entries and v is not None
        }
        failed: dict[str, list[str]] = {}
        for key, source in entries.items():
            errors = check_protected(source, translations.get(key))
            if errors:
                failed[key] = errors
        return translations, failed

    @staticmethod
    def _apply_fix(
        entries: dict[str, str],
        translations: dict[str, str],
        still_failed: dict[str, list[str]],
        key: str,
        errors: list[str],
        fixed: str | None,
    ) -> None:
        """Accept a refine result only when it improves the entry."""
        if fixed is not None:
            fixed = strip_invented_formatting(entries[key], fixed)
        new_errors = check_protected(entries[key], fixed)
        if not new_errors or len(new_errors) < len(errors):
            translations[key] = fixed or translations.get(key, "")
        if new_errors:
            still_failed[key] = new_errors

    def forward(
        self,
        source_lang: str,
        target_lang: str,
        context: str,
        glossary: str,
        entries: dict[str, str],
    ) -> dspy.Prediction:
        pred = self.translate(
            source_lang=source_lang,
            target_lang=target_lang,
            context=context,
            glossary=glossary,
            entries=entries,
        )
        translations, failed = self._initial_state(entries, pred)

        for attempt in range(self.max_refine):
            if not failed:
                break
            logger.debug(
                "Refine pass %d: %d failing entries", attempt + 1, len(failed)
            )
            still_failed: dict[str, list[str]] = {}
            for key, errors in failed.items():
                fixed = self.refine(
                    source=entries[key],
                    bad_translation=translations.get(key, ""),
                    validation_errors="; ".join(errors),
                    glossary=glossary,
                    target_lang=target_lang,
                ).fixed_translation
                self._apply_fix(entries, translations, still_failed, key, errors, fixed)
            failed = still_failed

        return dspy.Prediction(translations=translations, failed=failed)

    async def aforward(
        self,
        source_lang: str,
        target_lang: str,
        context: str,
        glossary: str,
        entries: dict[str, str],
    ) -> dspy.Prediction:
        pred = await self.translate.acall(
            source_lang=source_lang,
            target_lang=target_lang,
            context=context,
            glossary=glossary,
            entries=entries,
        )
        translations, failed = self._initial_state(entries, pred)

        for attempt in range(self.max_refine):
            if not failed:
                break
            logger.debug(
                "Refine pass %d: %d failing entries", attempt + 1, len(failed)
            )
            still_failed: dict[str, list[str]] = {}
            for key, errors in failed.items():
                fixed = (
                    await self.refine.acall(
                        source=entries[key],
                        bad_translation=translations.get(key, ""),
                        validation_errors="; ".join(errors),
                        glossary=glossary,
                        target_lang=target_lang,
                    )
                ).fixed_translation
                self._apply_fix(entries, translations, still_failed, key, errors, fixed)
            failed = still_failed

        return dspy.Prediction(translations=translations, failed=failed)


class GlossaryExtractor(dspy.Module):
    """Curate mined term candidates into glossary rules (one LLM call per
    candidate chunk; the orchestrator mines candidates deterministically)."""

    def __init__(self) -> None:
        super().__init__()
        self.curate = dspy.Predict(CurateGlossaryTerms)

    def forward(
        self,
        candidates: str,
        existing_glossary: str,
        target_lang: str,
        feedback: str = "",
    ) -> dspy.Prediction:
        pred = self.curate(
            candidates=candidates,
            existing_glossary=existing_glossary,
            target_lang=target_lang,
            feedback=feedback,
        )
        rules: list[TermRule] = list(pred.term_rules or [])
        return dspy.Prediction(term_rules=rules)

    async def aforward(
        self,
        candidates: str,
        existing_glossary: str,
        target_lang: str,
        feedback: str = "",
    ) -> dspy.Prediction:
        pred = await self.curate.acall(
            candidates=candidates,
            existing_glossary=existing_glossary,
            target_lang=target_lang,
            feedback=feedback,
        )
        rules: list[TermRule] = list(pred.term_rules or [])
        return dspy.Prediction(term_rules=rules)
