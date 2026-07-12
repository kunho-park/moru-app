"""LLM judge for semantic fluency — OFFLINE evaluation/optimization only.

Never wired into the runtime pipeline (runtime validation is free). The
judge LM is passed explicitly so it can be a large model independent of
the model under evaluation.
"""

from __future__ import annotations

import logging

import dspy

from ..dspy_modules.signatures import JudgeTranslationQuality

logger = logging.getLogger(__name__)


class LLMJudge:
    """Callable judge matching metrics.JudgeFn."""

    def __init__(self, lm: dspy.LM) -> None:
        self.lm = lm
        self.judge = dspy.Predict(JudgeTranslationQuality)

    def __call__(
        self, gold: dspy.Example, pred: dspy.Prediction
    ) -> tuple[float, list[str]]:
        entries: dict[str, str] = gold.entries
        translations: dict[str, str] = dict(getattr(pred, "translations", None) or {})
        scores: list[float] = []
        issues: list[str] = []
        with dspy.context(lm=self.lm):
            for key, source in entries.items():
                translated = translations.get(key)
                if not translated:
                    scores.append(0.0)
                    continue
                try:
                    verdict = self.judge(
                        source_text=source,
                        translated_text=translated,
                        target_lang=gold.target_lang,
                    )
                    score = max(0.0, min(1.0, float(verdict.score)))
                except (TypeError, ValueError) as exc:
                    logger.warning("Judge failed for key %s: %s", key, exc)
                    score = 0.0
                    verdict = None
                scores.append(score)
                if verdict is not None and score < 0.7 and verdict.issues:
                    issues.append(f"[{key}] fluency: {verdict.issues}")
        if not scores:
            return 0.0, issues
        return sum(scores) / len(scores), issues
