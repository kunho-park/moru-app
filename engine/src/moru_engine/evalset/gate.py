"""Adoption gate: paired LLM judging + cluster bootstrap CI (offline).

Regression prevention for the GEPA pipeline. A candidate program is
adopted for a language pair only when, on the held-out test split:

1. the position-randomized pairwise judge (PairwiseJudge) shows a mean
   improvement whose cluster-bootstrap CI lower bound clears the margin
   (clusters = examples, because entries inside one example share a
   rollout and are not independent);
2. the deterministic metric does not regress beyond ``det_epsilon``;
3. placeholder integrity and entry coverage are DETERMINISTIC
   INVARIANTS, not statistics: the candidate may not add a single
   failure over the baseline — checked on all entries AND separately on
   the production-packed "wide" stratum, so a wide-batch regression can
   never hide behind narrow-batch wins;
4. the judge actually scored enough entries (``min_judge_coverage``) —
   a mostly-failed judge run must never wave a candidate through.

Without a judge LM the gate falls back to the deterministic metric CI
(rule 1 applied to per-example metric deltas), with rules 2-3 unchanged.
"""

from __future__ import annotations

import logging
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

import dspy

from ..dspy_modules.translator import check_protected

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from .judge import PairwiseJudge

logger = logging.getLogger(__name__)

BOOTSTRAP_ITERS = 10_000
DEFAULT_ALPHA = 0.05
DEFAULT_MARGIN = 0.0
DEFAULT_DET_EPSILON = 0.01
DEFAULT_MIN_JUDGE_COVERAGE = 0.8

#: strata whose integrity is additionally gated in isolation
GUARDED_STRATA = ("wide",)


def rollout(
    program: dspy.Module,
    examples: Sequence[dspy.Example],
    *,
    lm: dspy.LM,
    metric: Callable,
    num_threads: int = 8,
) -> tuple[list[dspy.Prediction], list[float]]:
    """Run the program over examples; return (predictions, metric scores).

    Failures score 0.0 and yield an empty prediction, mirroring GEPA's
    failure_score semantics.
    """

    def scalar_metric(gold, pred, trace=None):
        return metric(gold, pred).score

    with dspy.context(lm=lm, adapter=dspy.JSONAdapter()):
        evaluator = dspy.Evaluate(
            devset=list(examples),
            metric=scalar_metric,
            num_threads=num_threads,
            display_progress=True,
            return_all_scores=True,
            failure_score=0.0,
            provide_traceback=True,
            max_errors=len(examples) * 100,
        )
        result = evaluator(program)
    predictions = [r[1] for r in result.results]
    scores = [float(r[2]) for r in result.results]
    return predictions, scores


@dataclass
class IntegrityStats:
    entries: int = 0
    placeholder_failures: int = 0
    coverage_misses: int = 0


def entry_integrity(
    examples: Sequence[dspy.Example],
    predictions: Sequence[dspy.Prediction],
) -> dict[str, IntegrityStats]:
    """Failure counts per stratum plus 'overall'.

    Placeholder failures count entries whose token multiset diverges from
    the source (check_protected); coverage misses count entries with no
    non-empty translation at all.
    """
    stats: dict[str, IntegrityStats] = {"overall": IntegrityStats()}
    for example, pred in zip(examples, predictions):
        stratum = str(getattr(example, "stratum", None) or "narrow")
        bucket = stats.setdefault(stratum, IntegrityStats())
        translations = dict(getattr(pred, "translations", None) or {})
        for key, source in example.entries.items():
            translated = translations.get(key)
            missing = not (translated and translated.strip())
            failed = bool(check_protected(source, translated))
            for scope in (stats["overall"], bucket):
                scope.entries += 1
                scope.coverage_misses += missing
                scope.placeholder_failures += failed
    return stats


def cluster_bootstrap(
    clusters: Sequence[Sequence[float]],
    *,
    iters: int = BOOTSTRAP_ITERS,
    alpha: float = DEFAULT_ALPHA,
    seed: int = 0,
) -> tuple[float, float, float] | None:
    """Percentile bootstrap of the mean, resampling whole clusters.

    Returns (mean, ci_low, ci_high) or None when there is no data.
    """
    populated = [list(c) for c in clusters if c]
    if not populated:
        return None
    values = [v for cluster in populated for v in cluster]
    mean = sum(values) / len(values)
    rng = random.Random(seed)
    n = len(populated)
    stats: list[float] = []
    for _ in range(iters):
        flat: list[float] = []
        for _ in range(n):
            flat.extend(populated[rng.randrange(n)])
        stats.append(sum(flat) / len(flat))
    stats.sort()
    lo_idx = int((alpha / 2) * iters)
    hi_idx = min(int((1 - alpha / 2) * iters), iters - 1)
    return mean, stats[lo_idx], stats[hi_idx]


def judge_pairs(
    judge: PairwiseJudge,
    examples: Sequence[dspy.Example],
    baseline_preds: Sequence[dspy.Prediction],
    candidate_preds: Sequence[dspy.Prediction],
    *,
    num_threads: int = 8,
) -> tuple[list[list[tuple[float, float]]], int]:
    """Pairwise-judge every entry; returns (per-example scores, n_total).

    Per-example lists hold (baseline, candidate) score tuples for entries
    the judge scored; failed judgments are dropped (see judge.py).
    """
    tasks: list[tuple[int, str, str, str, str, str | None, str | None]] = []
    for idx, (example, base_pred, cand_pred) in enumerate(
        zip(examples, baseline_preds, candidate_preds)
    ):
        base_translations = dict(getattr(base_pred, "translations", None) or {})
        cand_translations = dict(getattr(cand_pred, "translations", None) or {})
        for key, source in example.entries.items():
            tasks.append(
                (
                    idx,
                    key,
                    source,
                    example.translations.get(key, ""),
                    example.target_lang,
                    base_translations.get(key),
                    cand_translations.get(key),
                )
            )

    results: list[list[tuple[float, float]]] = [[] for _ in examples]

    def run(
        task: tuple[int, str, str, str, str, str | None, str | None],
    ) -> tuple[int, tuple[float, float] | None]:
        idx, key, source, reference, target_lang, baseline, candidate = task
        return idx, judge.compare(
            key=key,
            source=source,
            reference=reference,
            target_lang=target_lang,
            baseline=baseline,
            candidate=candidate,
        )

    with ThreadPoolExecutor(max_workers=num_threads) as pool:
        for idx, scored in pool.map(run, tasks):
            if scored is not None:
                results[idx].append(scored)
    return results, len(tasks)


@dataclass
class ArmStats:
    metric_mean: float
    integrity: dict[str, IntegrityStats]
    judge_mean: float | None = None


@dataclass
class GateDecision:
    pair: str
    n_examples: int
    n_entries: int
    n_judged: int
    baseline: ArmStats
    candidate: ArmStats
    judge_delta: float | None
    judge_ci: tuple[float, float] | None
    judge_wins: int
    judge_ties: int
    judge_losses: int
    metric_delta: float
    metric_ci: tuple[float, float] | None
    adopted: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _integrity_reasons(
    baseline: dict[str, IntegrityStats],
    candidate: dict[str, IntegrityStats],
) -> list[str]:
    """Zero-tolerance failure-count comparison, overall + guarded strata."""
    reasons: list[str] = []
    for scope in ("overall", *GUARDED_STRATA):
        base = baseline.get(scope)
        cand = candidate.get(scope)
        if base is None or cand is None:
            continue
        if cand.placeholder_failures > base.placeholder_failures:
            reasons.append(
                f"placeholder failures increased ({scope}): "
                f"{base.placeholder_failures} -> {cand.placeholder_failures}"
            )
        if cand.coverage_misses > base.coverage_misses:
            reasons.append(
                f"coverage misses increased ({scope}): "
                f"{base.coverage_misses} -> {cand.coverage_misses}"
            )
    return reasons


def decide(
    *,
    pair: str,
    examples: Sequence[dspy.Example],
    baseline_preds: Sequence[dspy.Prediction],
    baseline_scores: Sequence[float],
    candidate_preds: Sequence[dspy.Prediction],
    candidate_scores: Sequence[float],
    judge_scores: Sequence[Sequence[tuple[float, float]]] | None = None,
    n_judge_tasks: int = 0,
    margin: float = DEFAULT_MARGIN,
    alpha: float = DEFAULT_ALPHA,
    det_epsilon: float = DEFAULT_DET_EPSILON,
    min_judge_coverage: float = DEFAULT_MIN_JUDGE_COVERAGE,
    require_judge: bool = False,
    seed: int = 0,
) -> GateDecision:
    """Apply the adoption rules; every failed rule lands in ``reasons``.

    require_judge: when True, missing judge scores block adoption outright
    (the deterministic metric CI is advisory only) — set by tools whose
    adoption/confirmation step is defined as the pairwise-judge protocol.
    """
    base_integrity = entry_integrity(examples, baseline_preds)
    cand_integrity = entry_integrity(examples, candidate_preds)
    base_metric = sum(baseline_scores) / max(len(baseline_scores), 1)
    cand_metric = sum(candidate_scores) / max(len(candidate_scores), 1)
    metric_deltas = [[c - b] for b, c in zip(baseline_scores, candidate_scores)]
    metric_boot = cluster_bootstrap(metric_deltas, alpha=alpha, seed=seed)

    baseline_arm = ArmStats(base_metric, base_integrity)
    candidate_arm = ArmStats(cand_metric, cand_integrity)

    reasons: list[str] = []
    judge_delta = None
    judge_ci = None
    wins = ties = losses = 0
    n_judged = 0

    if judge_scores is not None:
        n_judged = sum(len(pairs) for pairs in judge_scores)
        base_vals = [b for pairs in judge_scores for b, _ in pairs]
        cand_vals = [c for pairs in judge_scores for _, c in pairs]
        if base_vals:
            baseline_arm.judge_mean = sum(base_vals) / len(base_vals)
            candidate_arm.judge_mean = sum(cand_vals) / len(cand_vals)
        for pairs in judge_scores:
            for b, c in pairs:
                if c > b:
                    wins += 1
                elif c < b:
                    losses += 1
                else:
                    ties += 1
        deltas = [[c - b for b, c in pairs] for pairs in judge_scores]
        boot = cluster_bootstrap(deltas, alpha=alpha, seed=seed)
        if boot is None:
            reasons.append("judge produced no scored entries")
        else:
            judge_delta, lo, hi = boot
            judge_ci = (lo, hi)
            if judge_delta <= 0:
                reasons.append(f"judge mean delta {judge_delta:+.4f} is not positive")
            if lo <= margin:
                reasons.append(
                    f"judge CI lower bound {lo:+.4f} does not clear margin "
                    f"{margin:+.4f}"
                )
        if n_judge_tasks > 0 and n_judged / n_judge_tasks < min_judge_coverage:
            reasons.append(
                f"judge coverage {n_judged}/{n_judge_tasks} below "
                f"{min_judge_coverage:.0%}"
            )
    else:
        if require_judge:
            reasons.append(
                "LLM judge required for adoption: pass --judge-model "
                "(deterministic metric CI alone cannot adopt)"
            )
        if metric_boot is None:
            reasons.append("no metric scores to compare")
        else:
            mean, lo, hi = metric_boot
            if mean <= 0:
                reasons.append(f"metric mean delta {mean:+.4f} is not positive")
            if lo <= margin:
                reasons.append(
                    f"metric CI lower bound {lo:+.4f} does not clear margin "
                    f"{margin:+.4f}"
                )

    if cand_metric < base_metric - det_epsilon:
        reasons.append(
            f"deterministic metric regressed: {cand_metric:.4f} < "
            f"{base_metric:.4f} - {det_epsilon}"
        )
    reasons.extend(_integrity_reasons(base_integrity, cand_integrity))

    n_entries = sum(len(ex.entries) for ex in examples)
    decision = GateDecision(
        pair=pair,
        n_examples=len(examples),
        n_entries=n_entries,
        n_judged=n_judged,
        baseline=baseline_arm,
        candidate=candidate_arm,
        judge_delta=judge_delta,
        judge_ci=judge_ci,
        judge_wins=wins,
        judge_ties=ties,
        judge_losses=losses,
        metric_delta=cand_metric - base_metric,
        metric_ci=None if metric_boot is None else (metric_boot[1], metric_boot[2]),
        adopted=not reasons,
        reasons=reasons,
    )
    logger.info(
        "Gate[%s]: adopted=%s judge_delta=%s metric %.4f -> %.4f%s",
        pair,
        decision.adopted,
        f"{judge_delta:+.4f}" if judge_delta is not None else "n/a",
        base_metric,
        cand_metric,
        ("; " + "; ".join(reasons)) if reasons else "",
    )
    return decision
