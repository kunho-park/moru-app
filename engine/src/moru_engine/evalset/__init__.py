"""Evaluation dataset building, metrics, judges, and the adoption gate."""

from __future__ import annotations

from .builder import (
    build_evalset,
    build_modtext_examples,
    build_stress_examples,
    build_vanilla_examples,
)
from .gate import GateDecision, cluster_bootstrap, decide, judge_pairs, rollout
from .judge import LLMJudge, PairwiseJudge
from .metrics import chrf_score, make_metric
from .modtext import harvest_pack, load_goldset

__all__ = [
    "GateDecision",
    "LLMJudge",
    "PairwiseJudge",
    "build_evalset",
    "build_modtext_examples",
    "build_stress_examples",
    "build_vanilla_examples",
    "chrf_score",
    "cluster_bootstrap",
    "decide",
    "harvest_pack",
    "judge_pairs",
    "load_goldset",
    "make_metric",
    "rollout",
]
