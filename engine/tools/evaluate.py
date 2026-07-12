"""Baseline/artifact evaluation runner (operator tool, no optimization).

Records the metric score of the current program (seed instructions or a
compiled artifact) on the held-out test split. Use this to log the
baseline before a GEPA run and to compare candidates afterwards.

Usage:
    uv run python tools/evaluate.py --model ollama_chat/qwen3.5:9b \
        --api-base http://localhost:11434
    uv run python tools/evaluate.py --model openai/gpt-4o-mini \
        --judge-model anthropic/claude-sonnet-4-5   # semantic component on
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import dspy  # noqa: E402

from moru_engine.dspy_modules import build_lm, load_translator  # noqa: E402
from moru_engine.evalset import LLMJudge, build_evalset, make_metric  # noqa: E402
from moru_engine.utils.log import setup_logging  # noqa: E402

logger = logging.getLogger("tools.evaluate")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="LiteLLM model under test")
    parser.add_argument("--api-base", default=None, help="override base URL (Ollama)")
    parser.add_argument("--judge-model", default=None, help="LLM judge model (optional)")
    parser.add_argument("--judge-api-base", default=None)
    parser.add_argument("--source", default="en_us")
    parser.add_argument("--target", default="ko_kr")
    parser.add_argument("--split", default="test", choices=["train", "val", "test"])
    parser.add_argument("--vanilla-samples", type=int, default=400)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--max-refine", type=int, default=2)
    args = parser.parse_args()

    setup_logging(logging.INFO)

    lm = build_lm(args.model, api_base=args.api_base)
    split = build_evalset(
        args.source, args.target, vanilla_samples=args.vanilla_samples, seed=args.seed
    )
    examples = split[args.split]
    logger.info("Evaluating on %s split: %d examples", args.split, len(examples))

    judge = (
        LLMJudge(build_lm(args.judge_model, api_base=args.judge_api_base))
        if args.judge_model
        else None
    )
    metric = make_metric(judge=judge)

    program, artifact_id = load_translator(
        args.model, args.source, args.target, max_refine=args.max_refine
    )

    def scalar_metric(gold, pred, trace=None):
        return metric(gold, pred).score

    with dspy.context(lm=lm, adapter=dspy.JSONAdapter()):
        evaluator = dspy.Evaluate(
            devset=examples,
            metric=scalar_metric,
            num_threads=args.threads,
            display_progress=True,
        )
        result = evaluator(program)

    raw = float(getattr(result, "score", result))
    score = raw / 100.0 if raw > 1.0 else raw
    print(
        json.dumps(
            {
                "model": args.model,
                "artifact": artifact_id or "seed-instructions",
                "split": args.split,
                "examples": len(examples),
                "judge": args.judge_model,
                "score": round(score, 4),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
