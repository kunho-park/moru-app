"""GEPA metric: deterministic score + rich text feedback (paper's mu / mu_f).

The score is FULLY DETERMINISTIC — no LLM anywhere in the scoring path:

    placeholder 0.35 / glossary 0.15 / format 0.10 / similarity(chrF) 0.40

dspy.GEPA calls the metric both for Pareto/full evaluations
(pred_name=None) and for reflective feedback (pred_name set). The score
is always the module-level score of the final program output — identical
on both paths — which keeps the optimizer's Pareto state free of judge
noise and satisfies dspy's score-consistency contract. When pred_name /
pred_trace are provided, only the TEXT feedback switches to a diagnosis
of that predictor's own trace output, so credit assignment survives the
refine loop (a translate mistake that refine later fixed must still be
visible to translate's reflection).

LLM judgment enters ONLY the offline adoption gate (evalset/gate.py) and
evaluate.py reporting — never this scoring path.

The similarity component is segment-level chrF (Popović 2015) against
the official vanilla reference translation, with protected {{KIND}}
tokens stripped (token integrity is the placeholder component's job).
Components that do not apply to an example (no glossary rules) are
dropped and the remaining weights renormalized, so constant free points
never dull candidate ranking.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

import dspy

from ..dspy_modules.translator import check_protected
from ..placeholder import TOKEN_RE

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MAX_LENGTH_RATIO = 3.0
MIN_LENGTH_RATIO = 0.2
MAX_FEEDBACK_ITEMS = 12

W_PLACEHOLDER = 0.35
W_GLOSSARY = 0.15
W_FORMAT = 0.10
W_SIMILARITY = 0.40

CHRF_CHAR_ORDER = 6
CHRF_BETA = 2.0
#: below this per-key chrF the feedback quotes the official reference
SIMILARITY_FEEDBACK_THRESHOLD = 0.70
MAX_SIMILARITY_CONTRASTS = 3

# English run of >= 3 letters inside parens/brackets, e.g. "경험치 (Experience)"
_PAREN_EN_RE = re.compile(r"[(\[]\s*([A-Za-z][A-Za-z0-9 '\-]{2,})\s*[)\]]")


def _alias_pattern(alias: str) -> re.Pattern[str]:
    return re.compile(
        r"(?<![a-z0-9_])" + re.escape(alias.lower()) + r"(?![a-z0-9_])"
    )


def chrf_score(
    hypothesis: str,
    reference: str,
    *,
    char_order: int = CHRF_CHAR_ORDER,
    beta: float = CHRF_BETA,
) -> float:
    """Segment-level chrF in [0, 1] (sacrebleu-default semantics).

    Precision/recall are averaged over the effective char orders (those
    where BOTH sides have n-grams) and F-beta is computed once, exactly
    as sacrebleu's ``CHRF._compute_f_score`` (char_order=6, word_order=0,
    beta=2, whitespace=False, eps_smoothing=False). Whitespace is removed
    entirely and protected ``{{KIND}}`` tokens are stripped first so
    trivially copied tokens cannot inflate similarity.
    """
    hyp = "".join(TOKEN_RE.sub(" ", hypothesis).split())
    ref = "".join(TOKEN_RE.sub(" ", reference).split())
    if not hyp and not ref:
        return 1.0
    if not hyp or not ref:
        return 0.0
    avg_precision = 0.0
    avg_recall = 0.0
    effective = 0
    for n in range(1, char_order + 1):
        n_hyp = len(hyp) - n + 1
        n_ref = len(ref) - n + 1
        if n_hyp <= 0 or n_ref <= 0:
            continue
        hyp_grams = Counter(hyp[i : i + n] for i in range(n_hyp))
        ref_grams = Counter(ref[i : i + n] for i in range(n_ref))
        match = sum((hyp_grams & ref_grams).values())
        avg_precision += match / n_hyp
        avg_recall += match / n_ref
        effective += 1
    if effective == 0:
        return 0.0
    avg_precision /= effective
    avg_recall /= effective
    if avg_precision + avg_recall == 0.0:
        return 0.0
    beta_sq = beta * beta
    return (
        (1 + beta_sq)
        * avg_precision
        * avg_recall
        / (beta_sq * avg_precision + avg_recall)
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
) -> tuple[float, list[str], int]:
    """Fraction of applicable (key, rule) checks that used the bound term.

    term_rules items: {"aliases": [str, ...], "target": str}.
    Returns (score, feedback, checks); checks == 0 means the component
    does not apply and must be dropped from the weighting.
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
        return 1.0, [], 0
    return passed / checks, feedback, checks


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


def similarity_component(
    entries: Mapping[str, str],
    translations: Mapping[str, str],
    gold_translations: Mapping[str, str],
) -> tuple[float, list[str]]:
    """Mean per-key chrF against the official reference translations.

    Missing translations score 0. Feedback quotes the official reference
    for the lowest-similarity keys so the reflection LM can contrast the
    model's wording with the gold standard (terminology + register).
    """
    if not entries:
        return 1.0, []
    per_key: dict[str, float] = {}
    for key in entries:
        translated = translations.get(key)
        gold = gold_translations.get(key, "")
        per_key[key] = chrf_score(translated, gold) if translated else 0.0
    worst = sorted(
        (score, key)
        for key, score in per_key.items()
        if score < SIMILARITY_FEEDBACK_THRESHOLD and translations.get(key)
    )[:MAX_SIMILARITY_CONTRASTS]
    feedback = [
        f"[{key}] low similarity to the official translation "
        f"(chrF {score:.2f}): yours '{translations.get(key, '')}' vs "
        f"official '{gold_translations.get(key, '')}'"
        for score, key in worst
    ]
    return sum(per_key.values()) / len(per_key), feedback


def _looks_like_text(text: str) -> bool:
    return sum(1 for c in text if c.isalpha()) >= 3


def _score_and_feedback(
    entries: Mapping[str, str],
    translations: Mapping[str, str],
    gold_translations: Mapping[str, str],
    term_rules: Sequence[Mapping[str, object]],
    target_lang: str,
) -> tuple[float, list[str], list[str]]:
    """Weighted deterministic score over one entry set.

    Returns (score, issue lines, component summary parts). Glossary weight
    applies only when at least one (key, rule) check fired.
    """
    s_ph, fb_ph = placeholder_component(entries, translations)
    s_gl, fb_gl, gl_checks = glossary_component(entries, translations, term_rules)
    s_fmt, fb_fmt = format_component(entries, translations, target_lang)
    s_sim, fb_sim = similarity_component(entries, translations, gold_translations)

    weighted: list[tuple[float, float]] = [
        (W_PLACEHOLDER, s_ph),
        (W_FORMAT, s_fmt),
        (W_SIMILARITY, s_sim),
    ]
    parts = [
        f"placeholder={s_ph:.2f}",
        f"format={s_fmt:.2f}",
        f"similarity={s_sim:.2f}",
    ]
    if gl_checks > 0:
        weighted.append((W_GLOSSARY, s_gl))
        parts.insert(1, f"glossary={s_gl:.2f}")
    total_weight = sum(w for w, _ in weighted)
    score = sum(w * s for w, s in weighted) / total_weight

    validator_issues = fb_ph + fb_gl + fb_fmt
    lines: list[str] = []
    issues = validator_issues + fb_sim
    if issues:
        lines.append("Problems to fix:")
        lines.extend(f"- {i}" for i in issues[:MAX_FEEDBACK_ITEMS])
        if len(issues) > MAX_FEEDBACK_ITEMS:
            lines.append(f"- ... and {len(issues) - MAX_FEEDBACK_ITEMS} more")
        failing = [k for k in entries if _key_failed(k, validator_issues)]
        for key in failing[:3]:
            ref = gold_translations.get(key)
            if ref:
                lines.append(f"Reference translation for [{key}]: {ref}")
    else:
        lines.append("All checks passed.")
    return score, lines, parts


def _field(obj: object, name: str) -> object:
    """Read a field from a Prediction or a plain dict."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _translate_trace_feedback(
    gold: dspy.Example,
    pred_inputs: Mapping[str, object],
    pred_output: object,
) -> list[str]:
    """Diagnose one translate call against ITS OWN inputs/outputs."""
    sub_entries = dict(pred_inputs.get("entries") or {})  # type: ignore[arg-type]
    raw = dict(_field(pred_output, "translations") or {})  # type: ignore[arg-type]
    if not sub_entries:
        return []
    _, lines, parts = _score_and_feedback(
        sub_entries,
        {k: v for k, v in raw.items() if k in sub_entries and v is not None},
        gold.translations,
        list(getattr(gold, "term_rules", None) or []),
        gold.target_lang,
    )
    header = (
        f"Diagnosis of THIS translate call ({len(sub_entries)} entries; "
        f"{', '.join(parts)}):"
    )
    return [header, *lines]


def _refine_trace_feedback(
    gold: dspy.Example,
    pred_inputs: Mapping[str, object],
    pred_output: object,
) -> list[str]:
    """Diagnose one refine call: did the fix resolve the listed errors?"""
    source = str(pred_inputs.get("source") or "")
    fixed = _field(pred_output, "fixed_translation")
    prior_errors = str(pred_inputs.get("validation_errors") or "")
    lines = [f"This refine call was asked to fix: {prior_errors}"]
    errors = check_protected(source, fixed if isinstance(fixed, str) else None)
    if errors:
        lines.append("The fix STILL fails validation:")
        lines.extend(f"- {e}" for e in errors)
    else:
        lines.append("The fix passes token validation.")
    key = next((k for k, v in gold.entries.items() if v == source), None)
    if key is not None:
        ref = gold.translations.get(key)
        if ref and isinstance(fixed, str) and fixed:
            sim = chrf_score(fixed, ref)
            lines.append(
                f"Similarity to the official translation: chrF {sim:.2f} "
                f"(yours '{fixed}' vs official '{ref}')"
            )
    return lines


def make_metric():
    """Build the GEPA metric closure (deterministic score + text feedback).

    The returned callable follows the GEPA feedback-metric protocol:
    (gold, pred, trace=None, pred_name=None, pred_trace=None) ->
    dspy.Prediction(score=float, feedback=str). The score is the
    module-level score of the final program output regardless of
    pred_name; only the feedback text is predictor-specific.
    """

    def metric(
        gold: dspy.Example,
        pred: dspy.Prediction,
        trace: object = None,
        pred_name: str | None = None,
        pred_trace: object = None,
    ) -> dspy.Prediction:
        entries: dict[str, str] = gold.entries
        translations: dict[str, str] = dict(getattr(pred, "translations", None) or {})
        term_rules = list(getattr(gold, "term_rules", None) or [])

        score, module_lines, parts = _score_and_feedback(
            entries, translations, gold.translations, term_rules, gold.target_lang
        )

        lines = [f"score={score:.3f} ({', '.join(parts)})"]
        trace_lines: list[str] = []
        if pred_name and pred_trace:
            try:
                _, pred_inputs, pred_output = pred_trace[0]
                if pred_name.startswith("refine"):
                    trace_lines = _refine_trace_feedback(gold, pred_inputs, pred_output)
                else:
                    trace_lines = _translate_trace_feedback(
                        gold, pred_inputs, pred_output
                    )
            except (IndexError, TypeError, ValueError, AttributeError):
                trace_lines = []
        lines.extend(trace_lines if trace_lines else module_lines)
        return dspy.Prediction(score=score, feedback="\n".join(lines))

    return metric


def _key_failed(key: str, issues: list[str]) -> bool:
    tag = f"[{key}]"
    return any(issue.startswith(tag) for issue in issues)
