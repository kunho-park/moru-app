"""Baseline/artifact evaluation runner (operator tool, no optimization).

Records the deterministic metric (placeholder/glossary/format/chrF vs the
official reference) of the current program — seed instructions or the
compiled artifact — on one split, with per-pair breakdown and integrity
failure counts. Use it to log the baseline before a GEPA run and to
inspect candidates afterwards.

--judge-model adds an absolute reference-based LLM judge column, reported
SEPARATELY from the metric (the metric stays deterministic everywhere).

Usage:
    uv run python tools/evaluate.py --model ollama_chat/qwen3.5:9b \
        --api-base http://192.168.0.241:11434 \
        --pairs en_us:ko_kr,en_us:ja_jp,en_us:zh_cn --split test \
        --save runs/baseline_test.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import dspy  # noqa: E402

from moru_engine.dspy_modules import build_lm, load_translator  # noqa: E402
from moru_engine.evalset import LLMJudge, build_evalset, make_metric, rollout  # noqa: E402
from moru_engine.evalset.builder import parse_pair_spec, slice_pair  # noqa: E402
from moru_engine.evalset.gate import entry_integrity  # noqa: E402
from moru_engine.utils.log import setup_logging  # noqa: E402

logger = logging.getLogger("tools.evaluate")


def parse_pairs(args: argparse.Namespace) -> list[tuple[str, str]]:
    try:
        return parse_pair_spec(args.pairs, args.source, args.target)
    except ValueError as exc:
        raise SystemExit(f"--pairs: {exc}") from exc


def judge_column(
    judge: LLMJudge,
    examples: list[dspy.Example],
    predictions: list[dspy.Prediction],
    threads: int,
) -> tuple[float | None, int]:
    """Mean absolute judge score over entries; failures excluded."""
    tasks = []
    for example, pred in zip(examples, predictions):
        translations = dict(getattr(pred, "translations", None) or {})
        for key, source in example.entries.items():
            tasks.append(
                (
                    source,
                    example.translations.get(key, ""),
                    translations.get(key),
                    example.target_lang,
                )
            )

    def run(task):
        source, reference, candidate, target_lang = task
        return judge.score_entry(
            source=source,
            reference=reference,
            candidate=candidate,
            target_lang=target_lang,
        )

    scores: list[float] = []
    with ThreadPoolExecutor(max_workers=threads) as pool:
        for result in pool.map(run, tasks):
            if result is not None:
                scores.append(result[0])
    if not scores:
        return None, 0
    return sum(scores) / len(scores), len(scores)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", required=True, help="LiteLLM model under test")
    parser.add_argument("--api-base", default=None, help="override base URL (Ollama)")
    parser.add_argument("--judge-model", default=None, help="absolute LLM judge (optional)")
    parser.add_argument("--judge-api-base", default=None)
    parser.add_argument("--source", default="en_us")
    parser.add_argument("--target", default="ko_kr")
    parser.add_argument(
        "--pairs",
        default=None,
        help="comma list of source:target pairs (overrides --source/--target)",
    )
    parser.add_argument(
        "--split",
        default="test",
        choices=["train", "val", "test", "confirmation"],
    )
    parser.add_argument("--vanilla-samples", type=int, default=900)
    parser.add_argument("--wide-samples", type=int, default=None)
    parser.add_argument(
        "--confirmation-samples",
        type=int,
        default=600,
        help="size of the reserved confirmation split (used only when "
        "--split confirmation)",
    )
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--max-refine", type=int, default=2)
    parser.add_argument("--save", default=None, help="write the full report JSON here")
    args = parser.parse_args()

    setup_logging(logging.INFO)

    pairs = parse_pairs(args)
    lm = build_lm(args.model, api_base=args.api_base)
    split = build_evalset(
        pairs=pairs,
        vanilla_samples=args.vanilla_samples,
        wide_samples=args.wide_samples,
        confirmation_samples=(
            args.confirmation_samples if args.split == "confirmation" else 0
        ),
        batch_size=args.batch_size,
        seed=args.seed,
    )
    examples_all = split[args.split]
    metric = make_metric()
    judge = (
        LLMJudge(build_lm(args.judge_model, api_base=args.judge_api_base, temperature=0.0))
        if args.judge_model
        else None
    )

    report: dict[str, object] = {
        "model": args.model,
        "split": args.split,
        "seed": args.seed,
        "vanilla_samples": args.vanilla_samples,
        "pairs": {},
    }
    for pair in pairs:
        pair_name = f"{pair[0]}-{pair[1]}"
        examples = slice_pair(examples_all, pair)
        program, artifact_id = load_translator(
            args.model, *pair, max_refine=args.max_refine
        )
        logger.info(
            "Evaluating %s on %d %s examples (%s)",
            pair_name,
            len(examples),
            args.split,
            artifact_id or "seed instructions",
        )
        predictions, scores = rollout(
            program, examples, lm=lm, metric=metric, num_threads=args.threads
        )
        integrity = entry_integrity(examples, predictions)
        pair_report: dict[str, object] = {
            "artifact": artifact_id,
            "n_examples": len(examples),
            "n_entries": sum(len(ex.entries) for ex in examples),
            "metric_mean": sum(scores) / max(len(scores), 1),
            "integrity": {name: asdict(stats) for name, stats in integrity.items()},
            "per_example": [
                {
                    "stratum": str(getattr(ex, "stratum", None) or "narrow"),
                    "keys": sorted(ex.entries),
                    "score": score,
                }
                for ex, score in zip(examples, scores)
            ],
        }
        if judge is not None:
            judge_mean, judged = judge_column(
                judge, examples, predictions, args.threads
            )
            pair_report["judge_mean"] = judge_mean
            pair_report["judge_entries"] = judged
        report["pairs"][pair_name] = pair_report  # type: ignore[index]

    compact = {
        pair: {
            k: v
            for k, v in data.items()  # type: ignore[union-attr]
            if k != "per_example"
        }
        for pair, data in report["pairs"].items()  # type: ignore[union-attr]
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))
    if args.save:
        save_path = Path(args.save)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("Report saved: %s", save_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
