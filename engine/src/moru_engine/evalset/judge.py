"""LLM judges — OFFLINE evaluation and adoption gate only.

Never wired into the runtime pipeline and never into the GEPA scoring
path (the optimizer's metric is fully deterministic; see metrics.py).
Two protocols:

- LLMJudge: absolute reference-based scoring of one candidate
  translation. Used by tools/evaluate.py for reporting.
- PairwiseJudge: position-randomized A/B comparison of two candidates
  against the official reference. Used by the adoption gate
  (evalset/gate.py) — pairwise judgments with randomized slots cancel
  the judge's positional and verbosity biases, and the shared reference
  anchors both arms to the official terminology.

Judge LMs are passed explicitly so they can be a model independent of
the model under evaluation. All calls run under dspy.context with a
JSONAdapter and retry once before giving up; a failed judgment returns
None and the caller excludes that entry (never a fake 0 that would
punish a candidate for infrastructure hiccups).
"""

from __future__ import annotations

import logging
import zlib

import dspy

from ..dspy_modules.signatures import JudgeTranslationPair, JudgeTranslationQuality

logger = logging.getLogger(__name__)

_MISSING_TEXT = "(no translation produced)"


def _clamp(value: object, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))  # type: ignore[arg-type]


class LLMJudge:
    """Absolute reference-based judge: one candidate, score in [0, 1]."""

    def __init__(self, lm: dspy.LM) -> None:
        self.lm = lm
        self.judge = dspy.Predict(JudgeTranslationQuality)

    def score_entry(
        self,
        *,
        source: str,
        reference: str,
        candidate: str | None,
        target_lang: str,
    ) -> tuple[float, str] | None:
        """Score one entry; None when the judge fails twice."""
        for attempt in (1, 2):
            try:
                with dspy.context(lm=self.lm, adapter=dspy.JSONAdapter()):
                    verdict = self.judge(
                        source_text=source,
                        reference_translation=reference,
                        candidate_translation=candidate or _MISSING_TEXT,
                        target_lang=target_lang,
                    )
                return _clamp(verdict.score, 0.0, 1.0), str(verdict.issues or "")
            except Exception as exc:  # noqa: BLE001 — LLM/parse errors
                logger.warning("Absolute judge attempt %d failed: %s", attempt, exc)
        return None

    def __call__(
        self, gold: dspy.Example, pred: dspy.Prediction
    ) -> tuple[float, list[str]]:
        """Average judge score over an example's entries.

        Missing translations score 0; failed judgments are excluded from
        the average (infrastructure noise must not look like quality).
        """
        entries: dict[str, str] = gold.entries
        translations: dict[str, str] = dict(getattr(pred, "translations", None) or {})
        scores: list[float] = []
        issues: list[str] = []
        for key, source in entries.items():
            translated = translations.get(key)
            if not translated:
                scores.append(0.0)
                continue
            result = self.score_entry(
                source=source,
                reference=gold.translations.get(key, ""),
                candidate=translated,
                target_lang=gold.target_lang,
            )
            if result is None:
                continue
            score, issue_text = result
            scores.append(score)
            if score < 0.7 and issue_text:
                issues.append(f"[{key}] quality: {issue_text}")
        if not scores:
            return 0.0, issues
        return sum(scores) / len(scores), issues


class PairwiseJudge:
    """Position-randomized paired judge for the adoption gate.

    The (baseline, candidate) pair is presented as anonymous slots A/B;
    which arm lands in slot A is a deterministic coin flip on the entry
    content, so a rerun reproduces the same prompts (and hits the LM
    disk cache) while the assignment stays balanced across entries.
    """

    def __init__(self, lm: dspy.LM) -> None:
        self.lm = lm
        self.judge = dspy.Predict(JudgeTranslationPair)

    @staticmethod
    def swap_slots(key: str, source: str) -> bool:
        """True when the baseline goes to slot B for this entry."""
        return bool(zlib.crc32(f"{key}|{source}".encode()) & 1)

    def compare(
        self,
        *,
        key: str,
        source: str,
        reference: str,
        target_lang: str,
        baseline: str | None,
        candidate: str | None,
    ) -> tuple[float, float] | None:
        """Score (baseline, candidate) in [0, 1]; None when judging fails."""
        base_text = baseline or _MISSING_TEXT
        cand_text = candidate or _MISSING_TEXT
        swap = self.swap_slots(key, source)
        slot_a, slot_b = (cand_text, base_text) if swap else (base_text, cand_text)
        for attempt in (1, 2):
            try:
                with dspy.context(lm=self.lm, adapter=dspy.JSONAdapter()):
                    verdict = self.judge(
                        source_text=source,
                        reference_translation=reference,
                        target_lang=target_lang,
                        translation_a=slot_a,
                        translation_b=slot_b,
                    )
                score_a = _clamp(verdict.score_a, 0.0, 10.0) / 10.0
                score_b = _clamp(verdict.score_b, 0.0, 10.0) / 10.0
                return (score_b, score_a) if swap else (score_a, score_b)
            except Exception as exc:  # noqa: BLE001 — LLM/parse errors
                logger.warning(
                    "Pairwise judge attempt %d failed for [%s]: %s",
                    attempt,
                    key,
                    exc,
                )
        return None
