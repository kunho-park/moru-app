"""GEPA optimization runner (OPERATOR TOOL — offline compile stage).

Never shipped in the app bundle and never run by end users. Compiles the
BatchTranslator program against the evalset and, when the optimized
program beats the current artifact on the held-out test split, writes the
artifact JSON for bundling into the next release.

Usage:
    uv run python tools/optimize.py \
        --model openai/gpt-4o-mini \
        --reflection-model anthropic/claude-sonnet-4-5 \
        --target ko_kr --auto light --threads 8

Requires provider API keys in the environment (e.g. OPENAI_API_KEY,
ANTHROPIC_API_KEY). Costs money: reflection-LM calls are the dominant
term (auto=light, a few hundred examples => single-digit dollars).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
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
from moru_engine.evalset import LLMJudge, build_evalset, make_metric  # noqa: E402
from moru_engine.utils.log import setup_logging  # noqa: E402

logger = logging.getLogger("tools.optimize")


def evaluate(
    program: dspy.Module,
    examples: list[dspy.Example],
    metric,
    num_threads: int,
) -> float:
    """Average metric score over examples (0..1)."""

    def scalar_metric(gold, pred, trace=None):
        return metric(gold, pred).score

    evaluator = dspy.Evaluate(
        devset=examples,
        metric=scalar_metric,
        num_threads=num_threads,
        display_progress=True,
    )
    result = evaluator(program)
    score = getattr(result, "score", result)
    return float(score) / 100.0 if float(score) > 1.0 else float(score)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="LiteLLM model under optimization")
    parser.add_argument(
        "--reflection-model",
        default="anthropic/claude-sonnet-4-5",
        help="large model for GEPA reflection",
    )
    parser.add_argument("--judge-model", default=None, help="LLM judge model (optional)")
    parser.add_argument("--api-base", default=None, help="override base URL for the model under optimization (Ollama)")
    parser.add_argument("--reflection-api-base", default=None)
    parser.add_argument("--judge-api-base", default=None)
    parser.add_argument("--source", default="en_us")
    parser.add_argument("--target", default="ko_kr")
    parser.add_argument("--auto", default="light", choices=["light", "medium", "heavy"])
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--vanilla-samples", type=int, default=400)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-refine", type=int, default=2)
    parser.add_argument(
        "--force", action="store_true", help="save artifact even if not better"
    )
    args = parser.parse_args()

    setup_logging(logging.INFO)

    lm = build_lm(args.model, api_base=args.api_base)
    configure_engine(lm)

    split = build_evalset(
        args.source,
        args.target,
        vanilla_samples=args.vanilla_samples,
        seed=args.seed,
    )
    train, val, test = split["train"], split["val"], split["test"]
    logger.info("Evalset: train=%d val=%d test=%d", len(train), len(val), len(test))

    judge = (
        LLMJudge(build_lm(args.judge_model, api_base=args.judge_api_base))
        if args.judge_model
        else None
    )
    metric = make_metric(judge=judge)

    program = BatchTranslator(max_refine=args.max_refine)
    tier = resolve_tier(args.model)
    out_path = artifact_path(tier, args.source, args.target)

    baseline_test = evaluate(program, test, metric, args.threads)
    logger.info("Baseline (seed instructions) test score: %.4f", baseline_test)

    current_best = baseline_test
    if out_path.exists():
        current = BatchTranslator(max_refine=args.max_refine)
        current.load(str(out_path))
        current_best = evaluate(current, test, metric, args.threads)
        logger.info("Existing artifact %s test score: %.4f", out_path.name, current_best)

    optimizer = dspy.GEPA(
        metric=metric,
        reflection_lm=build_lm(
            args.reflection_model,
            api_base=args.reflection_api_base,
            temperature=1.0,
            max_tokens=32000,
        ),
        auto=args.auto,
        num_threads=args.threads,
        track_stats=True,
    )
    optimized = optimizer.compile(program, trainset=train, valset=val)

    optimized_test = evaluate(optimized, test, metric, args.threads)
    logger.info("Optimized test score: %.4f (best so far %.4f)", optimized_test, current_best)

    report = {
        "model": args.model,
        "tier": tier,
        "pair": f"{args.source}-{args.target}",
        "baseline_test": baseline_test,
        "current_best_test": current_best,
        "optimized_test": optimized_test,
        "adopted": optimized_test > current_best or args.force,
    }
    print(json.dumps(report, indent=2))

    if optimized_test > current_best or args.force:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        optimized.save(str(out_path))
        logger.info("Artifact saved: %s", out_path)
        return 0
    logger.warning("Optimized program did not beat current best; artifact NOT saved")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
