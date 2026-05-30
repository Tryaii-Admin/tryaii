"""Cross-SDK parity guards.

The Node and Python SDKs are meant to make identical routing/scoring decisions.
They have silently drifted before (mismatched ARC scores, pricing, MT-Bench
ranges), so these tests fail loudly the moment the shipped data diverges again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tryaii_dre.benchmarks.standard import STANDARD_BENCHMARKS
from tryaii_dre.scoring.benchmarks import NORMALIZATION_RANGES

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_PRESET = (
    REPO_ROOT
    / "packages"
    / "node"
    / "src"
    / "registry"
    / "presets"
    / "defaultModels.json"
)
PY_PRESET = (
    REPO_ROOT
    / "packages"
    / "python"
    / "tryaii_dre"
    / "registry"
    / "presets"
    / "default_models.json"
)


def _by_model_id(raw: dict | list) -> dict[str, dict]:
    """Index a preset file by model_id, ignoring list ordering."""
    models = raw["models"] if isinstance(raw, dict) and "models" in raw else raw
    return {m["model_id"]: m for m in models}


def test_preset_model_data_identical_across_sdks():
    """The Node and Python default model presets must be byte-for-byte equal data.

    Pricing feeds cost scoring + the budget knapsack and benchmark scores feed
    quality scoring, so any divergence routes the same prompt to different models
    depending on the SDK language.
    """
    if not NODE_PRESET.exists():
        pytest.skip("Node preset not present (python-only checkout)")

    node = _by_model_id(json.loads(NODE_PRESET.read_text(encoding="utf-8")))
    py = _by_model_id(json.loads(PY_PRESET.read_text(encoding="utf-8")))

    assert set(node) == set(py), (
        f"Model id sets differ: only in node={sorted(set(node) - set(py))}, "
        f"only in python={sorted(set(py) - set(node))}"
    )

    diffs: list[str] = []
    for model_id in sorted(node):
        n, p = node[model_id], py[model_id]
        if n == p:
            continue
        if n.get("pricing") != p.get("pricing"):
            diffs.append(
                f"{model_id}.pricing: node={n.get('pricing')} py={p.get('pricing')}"
            )
        nb, pb = n.get("benchmark_scores", {}), p.get("benchmark_scores", {})
        for k in sorted(set(nb) | set(pb)):
            if nb.get(k) != pb.get(k):
                diffs.append(
                    f"{model_id}.benchmark_scores.{k}: "
                    f"node={nb.get(k)} py={pb.get(k)}"
                )
        if n.get("latency") != p.get("latency"):
            diffs.append(
                f"{model_id}.latency: node={n.get('latency')} py={p.get('latency')}"
            )

    assert not diffs, "Node/Python preset data diverged:\n" + "\n".join(diffs)


def test_standalone_ranges_match_standard_benchmarks():
    """The standalone NORMALIZATION_RANGES must agree with the routing path.

    STANDARD_BENCHMARKS is what the default router actually uses; the standalone
    BenchmarkNormalizer table must not disagree with it (the MT-Bench 5-vs-6 bug).
    """
    diffs: list[str] = []
    for bench in STANDARD_BENCHMARKS:
        standalone = NORMALIZATION_RANGES.get(bench.name)
        if standalone is None:
            continue
        if (standalone.min_score, standalone.max_score) != (
            bench.normalization.min_score,
            bench.normalization.max_score,
        ):
            diffs.append(
                f"{bench.name}: standalone=({standalone.min_score}, {standalone.max_score}) "
                f"standard=({bench.normalization.min_score}, {bench.normalization.max_score})"
            )
    assert not diffs, "NORMALIZATION_RANGES disagree with STANDARD_BENCHMARKS:\n" + "\n".join(diffs)
