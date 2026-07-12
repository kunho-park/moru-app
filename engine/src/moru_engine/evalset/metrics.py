"""GEPA-compatible metric: runtime validator checks promoted to score + feedback.

Component weights:
    placeholder 0.35 / glossary 0.25 / format 0.15 / semantic(judge) 0.25
Runtime validation never uses the judge (zero cost); the judge only runs
during offline evaluation/optimization. Without a judge the remaining
weights are renormalized.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

import dspy

from ..dspy_modules.translator import check_protected
from ..placeholder import TOKEN_RE

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MAX_LENGTH_RATIO = 3.0
MIN_LENGTH_RATIO = 0.2
MAX_FEEDBACK_ITEMS = 12

W_PLACEHOLDER = 0.35
W_GLOSSARY = 0.25
W_FORMAT = 0.15
W_SEMANTIC = 0.25

# English run of >= 3 letters inside parens/brackets, e.g. "경험치 (Experience)"
_PAREN_EN_RE = re.compile(r"[(\[]\s*([A-Za-z][A-Za-z0-9 '\-]{2,})\s*[)\]]")


class JudgeFn(Protocol):
    """Semantic judge: returns (score 0..1, issue strings)."""

    def __call__(
        self, gold: dspy.Example, pred: dspy.Prediction
    ) -> tuple[float, list[str]]: ...


def _alias_pattern(alias: str) -> re.Pattern[str]:
    return re.compile(
        r"(?<![a-z0-9_])" + re.escape(alias.lower()) + r"(?![a-z0-9_])"
    )


def placeholder_component(
    entries: Mapping[str, str],
    translations: Mapping[str, str],
) -> tuple[float, list[str]]:
    """Per-key token multiset integrity (mirrors restore() semantics)."""
    if not entries:
        return 1.0, []
    passed = 0
    feedback: list[str] = []
    for key, source in entries.items():
        errors = check_protected(source, translations.get(key))
        if errors:
            feedback.append(f"[{key}] " + "; ".join(errors))
        else:
            passed += 1
    return passed / len(entries), feedback


def glossary_component(
    entries: Mapping[str, str],
    translations: Mapping[str, str],
    term_rules: Sequence[Mapping[str, object]],
) -> tuple[float, list[str]]:
    """Fraction of applicable (key, rule) checks that used the bound term.

    term_rules items: {"aliases": [str, ...], "target": str}.
    Score is 1.0 when no rule applies.
    """
    checks = 0
    passed = 0
    feedback: list[str] = []
    compiled = [
        (
            [_alias_pattern(a) for a in rule.get("aliases", []) if isinstance(a, str)],
            str(rule.get("target", "")),
            ", ".join(str(a) for a in rule.get("aliases", [])),
        )
        for rule in term_rules
    ]
    for key, source in entries.items():
        translated = translations.get(key)
        if translated is None:
            continue
        source_l = source.lower()
        for patterns, target, alias_label in compiled:
            if not target or not any(p.search(source_l) for p in patterns):
                continue
            checks += 1
            if target in translated:
                passed += 1
            else:
                feedback.append(
                    f"[{key}] glossary violation: '{alias_label}' must be "
                    f"translated as '{target}'"
                )
    if checks == 0:
        return 1.0, []
    return passed / checks, feedback


def format_component(
    entries: Mapping[str, str],
    translations: Mapping[str, str],
    target_lang: str,
) -> tuple[float, list[str]]:
    """Length ratio, untranslated output, and source-English-in-parens."""
    if not entries:
        return 1.0, []
    passed = 0
    feedback: list[str] = []
    for key, source in entries.items():
        translated = translations.get(key)
        if not translated:
            # already fully penalized by the placeholder component
            continue
        issues: list[str] = []
        stripped_len = len(TOKEN_RE.sub("", source).strip())
        if stripped_len >= 4:
            ratio = len(translated) / max(len(source), 1)
            if ratio > MAX_LENGTH_RATIO:
                issues.append(f"translation too long (ratio {ratio:.1f})")
            elif ratio < MIN_LENGTH_RATIO:
                issues.append(f"translation too short (ratio {ratio:.1f})")
            if translated == source and _looks_like_text(source):
                issues.append("output identical to source (untranslated)")
        if target_lang == "ko_kr":
            for match in _PAREN_EN_RE.finditer(translated):
                snippet = match.group(1)
                if snippet.lower() in source.lower():
                    issues.append(
                        f"source English '{snippet}' repeated in parentheses; "
                        "write Korean only"
                    )
        if issues:
            feedback.append(f"[{key}] " + "; ".join(issues))
        else:
            passed += 1
    total = sum(1 for k in entries if translations.get(k))
    if total == 0:
        return 0.0, feedback
    return passed / total, feedback


def _looks_like_text(text: str) -> bool:
    return sum(1 for c in text if c.isalpha()) >= 3


def make_metric(judge: JudgeFn | None = None):
    """Build a GEPA metric closure.

    The returned callable follows the GEPA feedback-metric protocol:
    (gold, pred, trace=None, pred_name=None, pred_trace=None) ->
    dspy.Prediction(score=float, feedback=str).
    """

    def metric(
        gold: dspy.Example,
        pred: dspy.Prediction,
        trace: object = None,
        pred_name: str | None = None,
        pred_trace: object = None,
    ) -> dspy.Prediction:
        entries: dict[str, str] = gold.entries
        gold_translations: dict[str, str] = gold.translations
        translations: dict[str, str] = dict(getattr(pred, "translations", None) or {})
        term_rules = list(getattr(gold, "term_rules", None) or [])
        target_lang: str = gold.target_lang

        s_ph, fb_ph = placeholder_component(entries, translations)
        s_gl, fb_gl = glossary_component(entries, translations, term_rules)
        s_fmt, fb_fmt = format_component(entries, translations, target_lang)

        if judge is not None:
            s_sem, fb_sem = judge(gold, pred)
            score = (
                W_PLACEHOLDER * s_ph
                + W_GLOSSARY * s_gl
                + W_FORMAT * s_fmt
                + W_SEMANTIC * s_sem
            )
        else:
            fb_sem = []
            base = W_PLACEHOLDER + W_GLOSSARY + W_FORMAT
            score = (
                W_PLACEHOLDER * s_ph + W_GLOSSARY * s_gl + W_FORMAT * s_fmt
            ) / base

        issues = fb_ph + fb_gl + fb_fmt + fb_sem
        lines = [
            f"score={score:.3f} (placeholder={s_ph:.2f}, glossary={s_gl:.2f}, "
            f"format={s_fmt:.2f}"
            + (f", semantic={s_sem:.2f})" if judge is not None else ")")
        ]
        if issues:
            lines.append("Problems to fix:")
            lines.extend(f"- {i}" for i in issues[:MAX_FEEDBACK_ITEMS])
            if len(issues) > MAX_FEEDBACK_ITEMS:
                lines.append(f"- ... and {len(issues) - MAX_FEEDBACK_ITEMS} more")
            failing = [k for k in entries if _key_failed(k, issues)]
            for key in failing[:3]:
                ref = gold_translations.get(key)
                if ref:
                    lines.append(f"Reference translation for [{key}]: {ref}")
        else:
            lines.append("All checks passed.")
        return dspy.Prediction(score=score, feedback="\n".join(lines))

    return metric


def _key_failed(key: str, issues: list[str]) -> bool:
    tag = f"[{key}]"
    return any(issue.startswith(tag) for issue in issues)
