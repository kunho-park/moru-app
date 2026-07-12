"""GEPA optimization runner (OPERATOR TOOL — offline compile stage).

Never shipped in the app bundle and never run by end users. Implements
the full loop from the GEPA paper (arXiv:2507.19457) per language pair:

1. Build the evalset with a key-level 60/20/20 split (test held out;
   narrow strata for statistics + production-packed wide strata).
2. Student = the shipping artifact when present (incumbent), else the
   seed program — optimization always continues from what users run.
3. dspy.GEPA evolves the predictor instructions (reflective mutation +
   Pareto candidate selection + merge) against the DETERMINISTIC metric
   (placeholder/glossary/format/chrF-vs-official-reference). Checkpoints
   land in --run-dir; rerunning with the same dir resumes.
4. Adoption gate (moru_engine.evalset.gate): the position-randomized
   pairwise LLM judge compares candidate vs incumbent on the held-out
   test split; adopt only on a confident win (cluster-bootstrap CI lower
   bound > margin) with no deterministic-metric, placeholder, or
   coverage regression. Rejected candidates never touch artifacts/.

Exit codes: 0 = every pair adopted, 2 = some pairs adopted, 1 = none.

Usage:
    uv run python tools/optimize.py \
        --model ollama_chat/qwen3.5:9b --api-base http://192.168.0.241:11434 \
        --reflection-model ollama_chat/qwen3.5:9b \
        --judge-model ollama_chat/qwen3.5:9b \
        --pairs en_us:ko_kr,en_us:ja_jp,en_us:zh_cn \
        --vanilla-samples 900 --max-metric-calls 500 --threads 8

Requires provider API keys in the environment for hosted models (e.g.
OPENAI_API_KEY, ANTHROPIC_API_KEY). Reflection-LM calls are the dominant
cloud cost (auto=light => single-digit dollars); a strong reflection
model is recommended when available.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import dspy  # noqa: E402

from moru_engine.dspy_modules import (  # noqa: E402
    BatchTranslator,
    artifact_path,
    build_lm,
    configure_engine,
    resolve_tier,
)
from moru_engine.evalset import (  # noqa: E402
    build_evalset,
    decide,
    judge_pairs,
    make_metric,
    rollout,
)
from moru_engine.evalset.builder import slice_pair  # noqa: E402
from moru_engine.evalset.judge import PairwiseJudge  # noqa: E402
from moru_engine.utils.log import setup_logging  # noqa: E402

logger = logging.getLogger("tools.optimize")


def parse_pairs(args: argparse.Namespace) -> list[tuple[str, str]]:
    if args.pairs:
        pairs: list[tuple[str, str]] = []
        for chunk in args.pairs.split(","):
            source, _, target = chunk.strip().partition(":")
            if not source or not target:
                raise SystemExit(f"--pairs entry '{chunk}' must be 'source:target'")
            pairs.append((source, target))
        return pairs
    return [(args.source, args.target)]



def load_incumbent(
    path: Path, max_refine: int
) -> tuple[BatchTranslator, str | None]:
    """(program, artifact name) — artifact when present, else seed."""
    program = BatchTranslator(max_refine=max_refine)
    if path.exists():
        program.load(str(path))
        return program, path.name
    return program, None


def instructions_of(program: dspy.Module) -> dict[str, str]:
    return {
        name: pred.signature.instructions
        for name, pred in program.named_predictors()
    }


def gepa_stats_payload(optimized: dspy.Module) -> dict[str, object] | None:
    stats = getattr(optimized, "detailed_results", None)
    if stats is None:
        return None
    return {
        "best_idx": stats.best_idx,
        "val_aggregate_scores": stats.val_aggregate_scores,
        "parents": stats.parents,
        "discovery_eval_counts": stats.discovery_eval_counts,
        "total_metric_calls": stats.total_metric_calls,
        "num_full_val_evals": stats.num_full_val_evals,
        "candidates": [instructions_of(c) for c in stats.candidates],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", required=True, help="LiteLLM model under optimization")
    parser.add_argument("--api-base", default=None, help="override base URL for the model under optimization (Ollama)")
    parser.add_argument(
        "--reflection-model",
        default="anthropic/claude-sonnet-4-5",
        help="large model for GEPA reflection",
    )
    parser.add_argument("--reflection-api-base", default=None)
    parser.add_argument("--reflection-max-tokens", type=int, default=32000)
    parser.add_argument(
        "--reflection-effort",
        default=None,
        help="reasoning_effort for the reflection LM (e.g. medium to enable "
        "thinking on local models; default: provider default / off for Ollama)",
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="LLM judge for the adoption gate (paired comparison); without "
        "it the gate falls back to the deterministic metric CI",
    )
    parser.add_argument("--judge-api-base", default=None)
    parser.add_argument("--source", default="en_us")
    parser.add_argument("--target", default="ko_kr")
    parser.add_argument(
        "--pairs",
        default=None,
        help="comma list of source:target pairs, e.g. "
        "en_us:ko_kr,en_us:ja_jp,en_us:zh_cn (overrides --source/--target)",
    )
    budget = parser.add_mutually_exclusive_group()
    budget.add_argument("--auto", default=None, choices=["light", "medium", "heavy"])
    budget.add_argument(
        "--max-metric-calls",
        type=int,
        default=None,
        help="explicit GEPA rollout budget per pair (recommended for local models)",
    )
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--vanilla-samples", type=int, default=900, help="narrow-strata entries per pair")
    parser.add_argument("--wide-samples", type=int, default=None, help="production-packed entries per pair (default vanilla//3)")
    parser.add_argument("--batch-size", type=int, default=6, help="narrow-strata entries per example")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-refine", type=int, default=2)
    parser.add_argument("--reflection-minibatch", type=int, default=3)
    parser.add_argument("--no-merge", action="store_true", help="disable GEPA merge proposals")
    parser.add_argument("--margin", type=float, default=0.0, help="CI lower bound must exceed this to adopt")
    parser.add_argument("--alpha", type=float, default=0.05, help="bootstrap CI significance level")
    parser.add_argument("--run-dir", default=None, help="checkpoint/report dir (default runs/gepa_<ts>)")
    parser.add_argument(
        "--force", action="store_true", help="save artifacts even when the gate rejects"
    )
    args = parser.parse_args()

    setup_logging(logging.INFO)
    if args.auto is None and args.max_metric_calls is None:
        args.auto = "light"

    run_dir = Path(args.run_dir or f"runs/gepa_{time.strftime('%Y%m%d_%H%M%S')}")
    run_dir.mkdir(parents=True, exist_ok=True)

    pairs = parse_pairs(args)
    lm = build_lm(args.model, api_base=args.api_base)
    configure_engine(lm)

    reflection_extra: dict[str, object] = {}
    if args.reflection_effort:
        reflection_extra["reasoning_effort"] = args.reflection_effort
    reflection_lm = build_lm(
        args.reflection_model,
        api_base=args.reflection_api_base,
        temperature=1.0,
        max_tokens=args.reflection_max_tokens,
        **reflection_extra,
    )
    judge = (
        PairwiseJudge(
            build_lm(args.judge_model, api_base=args.judge_api_base, temperature=0.0)
        )
        if args.judge_model
        else None
    )
    if judge is None:
        logger.warning(
            "No --judge-model: adoption gate will use the deterministic "
            "metric CI only"
        )

    split = build_evalset(
        pairs=pairs,
        vanilla_samples=args.vanilla_samples,
        wide_samples=args.wide_samples,
        batch_size=args.batch_size,
        seed=args.seed,
    )
    metric = make_metric()
    tier = resolve_tier(args.model)

    decisions = []
    report: dict[str, object] = {
        "model": args.model,
        "tier": tier,
        "reflection_model": args.reflection_model,
        "judge_model": args.judge_model,
        "vanilla_samples": args.vanilla_samples,
        "batch_size": args.batch_size,
        "seed": args.seed,
        "budget": {"auto": args.auto, "max_metric_calls": args.max_metric_calls},
        "margin": args.margin,
        "alpha": args.alpha,
        "pairs": {},
    }

    for pair in pairs:
        pair_name = f"{pair[0]}-{pair[1]}"
        train = slice_pair(split["train"], pair)
        val = slice_pair(split["val"], pair)
        test = slice_pair(split["test"], pair)
        logger.info(
            "=== Pair %s: train=%d val=%d test=%d examples ===",
            pair_name,
            len(train),
            len(val),
            len(test),
        )

        out_path = artifact_path(tier, *pair)
        baseline, incumbent_name = load_incumbent(out_path, args.max_refine)
        student = baseline.deepcopy()
        logger.info(
            "Student starts from %s",
            incumbent_name or "seed instructions (no artifact)",
        )

        optimizer = dspy.GEPA(
            metric=metric,
            reflection_lm=reflection_lm,
            auto=args.auto,
            max_metric_calls=args.max_metric_calls,
            reflection_minibatch_size=args.reflection_minibatch,
            candidate_selection_strategy="pareto",
            use_merge=not args.no_merge,
            num_threads=args.threads,
            failure_score=0.0,
            perfect_score=1.0,
            log_dir=str(run_dir / pair_name / "gepa"),
            track_stats=True,
            seed=args.seed,
        )
        optimized = optimizer.compile(student, trainset=train, valset=val)

        logger.info("Gate rollout: baseline on %d test examples", len(test))
        base_preds, base_scores = rollout(
            baseline, test, lm=lm, metric=metric, num_threads=args.threads
        )
        logger.info("Gate rollout: candidate on %d test examples", len(test))
        cand_preds, cand_scores = rollout(
            optimized, test, lm=lm, metric=metric, num_threads=args.threads
        )
        judge_scores = None
        n_judge_tasks = 0
        if judge is not None:
            logger.info("Pairwise judging %d test examples", len(test))
            judge_scores, n_judge_tasks = judge_pairs(
                judge,
                test,
                base_preds,
                cand_preds,
                num_threads=args.threads,
            )

        decision = decide(
            pair=pair_name,
            examples=test,
            baseline_preds=base_preds,
            baseline_scores=base_scores,
            candidate_preds=cand_preds,
            candidate_scores=cand_scores,
            judge_scores=judge_scores,
            n_judge_tasks=n_judge_tasks,
            margin=args.margin,
            alpha=args.alpha,
            seed=args.seed,
        )
        decisions.append(decision)

        pair_report: dict[str, object] = {
            "artifact": str(out_path),
            "incumbent": incumbent_name,
            "baseline_instructions": instructions_of(baseline),
            "optimized_instructions": instructions_of(optimized),
            "gepa": gepa_stats_payload(optimized),
            "decision": decision.to_dict(),
        }
        report["pairs"][pair_name] = pair_report  # type: ignore[index]

        if decision.adopted or args.force:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            optimized.save(str(out_path))
            logger.info("Artifact saved: %s", out_path)
        else:
            logger.warning(
                "Pair %s NOT adopted: %s", pair_name, "; ".join(decision.reasons)
            )

        # incremental report flush so a crash never loses finished pairs
        (run_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    adopted = sum(1 for d in decisions if d.adopted or args.force)
    summary = {
        "run_dir": str(run_dir),
        "adopted_pairs": adopted,
        "total_pairs": len(decisions),
        "decisions": [
            {
                "pair": d.pair,
                "adopted": bool(d.adopted or args.force),
                "judge_delta": d.judge_delta,
                "judge_ci": d.judge_ci,
                "metric_delta": d.metric_delta,
                "reasons": d.reasons,
            }
            for d in decisions
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if adopted == len(decisions):
        return 0
    return 2 if adopted else 1


if __name__ == "__main__":
    raise SystemExit(main())
