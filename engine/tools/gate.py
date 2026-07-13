"""Standalone adoption gate (OPERATOR TOOL — candidate re-validation).

Validates ANY candidate program file (e.g. a hand-tweaked instruction or
an externally produced prompt) against the shipping baseline with the
EXACT protocol tools/optimize.py uses: position-randomized pairwise LLM
judging, cluster-bootstrap CI, deterministic-metric non-regression, and
zero-tolerance placeholder/coverage failure counts. A manual edit must
never ship by overwriting the artifact directly — an edited file is no
longer the candidate those guarantees were computed for.

Holdout discipline:
- Iterate on the VALIDATION split (default) as often as you like while
  tuning a candidate.
- optimize.py has already spent the regular test split on its own
  adoption decision, so hand-tweaked candidates get their ONE final
  confirmatory answer from a separate CONFIRMATION split: keys from the
  test bucket that no other split (including the test examples) ever
  touched. Run --final once per candidate lineage; repeatedly picking
  whichever edit passes turns the holdout into a validation set and
  voids the CI. The tool warns when earlier --final reports exist.
- --apply (install into artifacts/) requires --final AND an adopted
  verdict; the validated file is copied byte-identical.

Exit codes: 0 = every pair adopted, 2 = some, 1 = none.

Usage:
    # iterate freely on the validation split
    uv run python tools/gate.py --model openrouter/openai/gpt-5.6-luna \
        --candidate edited.json --pairs en_us:ko_kr \
        --judge-model openrouter/google/gemini-3.5-flash

    # single confirmatory decision, then install on adoption
    uv run python tools/gate.py --model openrouter/openai/gpt-5.6-luna \
        --candidate edited.json --pairs en_us:ko_kr \
        --judge-model openrouter/google/gemini-3.5-flash --final --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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
from moru_engine.evalset.builder import parse_pair_spec, slice_pair  # noqa: E402
from moru_engine.evalset.judge import PairwiseJudge  # noqa: E402
from moru_engine.utils.log import setup_logging  # noqa: E402

logger = logging.getLogger("tools.gate")


def prior_final_reports(pair_name: str) -> list[str]:
    """Earlier --final gate reports mentioning this pair (reuse nudge)."""
    hits: list[str] = []
    for report_path in sorted(Path("runs").glob("gate_final_*/report.json")):
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if pair_name in data.get("pairs", {}):
            hits.append(str(report_path))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", required=True, help="LiteLLM task model")
    parser.add_argument("--api-base", default=None)
    parser.add_argument(
        "--candidate", required=True, help="candidate program JSON to validate"
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="baseline program JSON (default: shipping artifact, else seed)",
    )
    parser.add_argument("--judge-model", default=None, help="pairwise gate judge")
    parser.add_argument("--judge-api-base", default=None)
    parser.add_argument("--source", default="en_us")
    parser.add_argument("--target", default="ko_kr")
    parser.add_argument("--pairs", default=None, help="comma list of source:target")
    parser.add_argument("--vanilla-samples", type=int, default=900)
    parser.add_argument("--wide-samples", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--max-refine", type=int, default=2)
    parser.add_argument("--margin", type=float, default=0.0)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument(
        "--final",
        action="store_true",
        help="confirmatory decision on the reserved CONFIRMATION split "
        "(untouched test-bucket keys; once per candidate lineage); "
        "default gates on the validation split",
    )
    parser.add_argument(
        "--confirmation-samples",
        type=int,
        default=600,
        help="narrow entries per pair reserved for the confirmation split "
        "(plus //3 production-packed)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="on adoption, install the candidate file into artifacts/ "
        "(requires --final)",
    )
    parser.add_argument("--run-dir", default=None)
    args = parser.parse_args()

    if args.apply and not args.final:
        parser.error("--apply requires --final (confirmation-split decision)")
    if args.final and not args.judge_model:
        parser.error(
            "--final requires --judge-model: the confirmatory gate IS the "
            "pairwise LLM-judge protocol"
        )

    setup_logging(logging.INFO)
    try:
        pairs = parse_pair_spec(args.pairs, args.source, args.target)
    except ValueError as exc:
        raise SystemExit(f"--pairs: {exc}") from exc

    candidate_path = Path(args.candidate)
    if not candidate_path.exists():
        raise SystemExit(f"candidate not found: {candidate_path}")

    split_name = "confirmation" if args.final else "val"
    kind = "final" if args.final else "val"
    run_dir = Path(args.run_dir or f"runs/gate_{kind}_{time.strftime('%Y%m%d_%H%M%S')}")
    run_dir.mkdir(parents=True, exist_ok=True)

    lm = build_lm(args.model, api_base=args.api_base)
    configure_engine(lm)
    judge = (
        PairwiseJudge(
            build_lm(args.judge_model, api_base=args.judge_api_base, temperature=0.0)
        )
        if args.judge_model
        else None
    )
    if judge is None:
        logger.warning(
            "No --judge-model: validation-mode stats use the deterministic "
            "metric CI only (a --final decision always requires the judge)"
        )

    split = build_evalset(
        pairs=pairs,
        vanilla_samples=args.vanilla_samples,
        wide_samples=args.wide_samples,
        confirmation_samples=args.confirmation_samples if args.final else 0,
        batch_size=args.batch_size,
        seed=args.seed,
    )
    metric = make_metric()
    tier = resolve_tier(args.model)

    decisions = []
    report: dict[str, object] = {
        "model": args.model,
        "candidate": str(candidate_path),
        "baseline": args.baseline,
        "judge_model": args.judge_model,
        "split": split_name,
        "seed": args.seed,
        "margin": args.margin,
        "alpha": args.alpha,
        "confirmation_samples_requested": (
            args.confirmation_samples + args.confirmation_samples // 3
            if args.final
            else None
        ),
        "pairs": {},
    }

    for pair in pairs:
        pair_name = f"{pair[0]}-{pair[1]}"
        if args.final:
            earlier = prior_final_reports(pair_name)
            if earlier:
                logger.warning(
                    "TEST split already consulted %d time(s) for %s (%s). "
                    "Repeated final gating of hand-picked candidates turns the "
                    "holdout into a validation set and voids the CI guarantee.",
                    len(earlier),
                    pair_name,
                    ", ".join(earlier[-3:]),
                )

        examples = slice_pair(split[split_name], pair)
        ship_path = artifact_path(tier, *pair)
        baseline = BatchTranslator(max_refine=args.max_refine)
        baseline_name = "seed instructions"
        if args.baseline:
            baseline.load(args.baseline)
            baseline_name = args.baseline
        elif ship_path.exists():
            baseline.load(str(ship_path))
            baseline_name = ship_path.name
        candidate = BatchTranslator(max_refine=args.max_refine)
        candidate.load(str(candidate_path))

        logger.info(
            "Gate[%s] on %s split (%d examples): %s vs %s",
            pair_name,
            split_name,
            len(examples),
            baseline_name,
            candidate_path.name,
        )
        base_preds, base_scores = rollout(
            baseline, examples, lm=lm, metric=metric, num_threads=args.threads
        )
        cand_preds, cand_scores = rollout(
            candidate, examples, lm=lm, metric=metric, num_threads=args.threads
        )
        judge_scores = None
        n_judge_tasks = 0
        if judge is not None:
            judge_scores, n_judge_tasks = judge_pairs(
                judge, examples, base_preds, cand_preds, num_threads=args.threads
            )

        decision = decide(
            pair=pair_name,
            examples=examples,
            baseline_preds=base_preds,
            baseline_scores=base_scores,
            candidate_preds=cand_preds,
            candidate_scores=cand_scores,
            judge_scores=judge_scores,
            n_judge_tasks=n_judge_tasks,
            margin=args.margin,
            alpha=args.alpha,
            require_judge=args.final,
            seed=args.seed,
        )
        decisions.append(decision)
        report["pairs"][pair_name] = {  # type: ignore[index]
            "baseline": baseline_name,
            "n_entries_actual": decision.n_entries,
            "decision": decision.to_dict(),
        }

        if decision.adopted and args.apply:
            ship_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidate_path, ship_path)
            logger.info("Candidate installed byte-identical: %s", ship_path)
        elif args.apply:
            logger.warning(
                "Pair %s NOT adopted — artifact untouched: %s",
                pair_name,
                "; ".join(decision.reasons),
            )

        (run_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    adopted = sum(1 for d in decisions if d.adopted)
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "split": split_name,
                "adopted_pairs": adopted,
                "total_pairs": len(decisions),
                "decisions": [
                    {
                        "pair": d.pair,
                        "adopted": d.adopted,
                        "judge_delta": d.judge_delta,
                        "judge_ci": d.judge_ci,
                        "metric_delta": d.metric_delta,
                        "reasons": d.reasons,
                    }
                    for d in decisions
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if adopted == len(decisions):
        return 0
    return 2 if adopted else 1


if __name__ == "__main__":
    raise SystemExit(main())
