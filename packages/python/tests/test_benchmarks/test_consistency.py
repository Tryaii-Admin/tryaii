"""Cross-source benchmark-name consistency guard for every catalog bundle.

Benchmark NAMES are the join key between the five files of a catalog bundle:
the taxonomy (benchmarks.json), the normalization ranges, the classifier
centroids, the training queries, and the model scores (models.json). When they
drift, prompts can match benchmarks no model carries data for and score zero
models -- which would make an entire budget eval report "infeasible". These
tests run on the packaged starter bundle and, when built, the full bundle, and
fail loudly the moment any source drifts.

Mirrors packages/node/tests/benchmarks/consistency.test.ts.
"""

from __future__ import annotations

import math

import pytest

from tryaii.benchmarks.registry import BenchmarkRegistry
from tryaii.benchmarks.standard import STANDARD_BENCHMARKS
from tryaii.catalog import starter_bundle
from tryaii.centroids.generator import benchmark_fingerprint
from tryaii.classifiers.embedding import BENCHMARK_CATEGORIES
from tryaii.scoring.benchmarks import (
    BENCHMARK_WEIGHTS,
    NORMALIZATION_RANGES,
    RANDOM_CHANCE_FLOORS,
)

from ..conftest import load_full_bundle


@pytest.fixture(params=["starter", "full"])
def bundle(request):
    return starter_bundle() if request.param == "starter" else load_full_bundle()


def _registry_benchmark_names(bundle) -> set[str]:
    """Union of every benchmark name any model in the bundle carries a score for."""
    names: set[str] = set()
    for model in bundle.model_entries():
        names.update((model.get("benchmark_scores") or {}).keys())
    return names


def _centroid_names(bundle) -> set[str]:
    return set(bundle.centroids["centroids"].keys())


def _training_query_names(bundle) -> set[str]:
    return set(bundle.training_queries["benchmarks"].keys())


def _range_names(bundle) -> set[str]:
    return set(bundle.range_entries())


class TestBundleConsistency:
    def test_centroid_keys_match_training_query_keys(self, bundle):
        assert _centroid_names(bundle) == _training_query_names(bundle)

    def test_taxonomy_names_match_range_keys(self, bundle):
        names = bundle.benchmark_names
        assert len(names) == len(set(names)), "duplicate benchmarks.json names"
        assert set(names) == _range_names(bundle)

    def test_every_registry_benchmark_has_a_normalization_range(self, bundle):
        missing = _registry_benchmark_names(bundle) - _range_names(bundle)
        assert missing == set(), f"registry benchmarks missing a range: {sorted(missing)}"

    def test_every_centroid_benchmark_has_a_normalization_range(self, bundle):
        missing = _centroid_names(bundle) - _range_names(bundle)
        assert missing == set(), f"centroid benchmarks missing a range: {sorted(missing)}"

    def test_every_benchmark_weight_is_positive_finite(self, bundle):
        bad = {
            name: w for name, w in bundle.benchmark_weights().items()
            if not math.isfinite(w) or w <= 0
        }
        assert bad == {}, f"non-positive/non-finite weights: {bad}"

    def test_every_centroid_benchmark_resolves_to_a_category(self, bundle):
        missing = _centroid_names(bundle) - set(bundle.benchmark_categories())
        assert missing == set(), f"centroid benchmarks missing a category: {sorted(missing)}"

    def test_every_benchmark_has_a_known_family(self, bundle):
        families = {f["id"] for f in bundle.benchmarks.get("families", [])}
        unknown = {e["name"]: e["family"] for e in bundle.benchmark_entries
                   if e["family"] not in families}
        assert unknown == {}, f"benchmarks with an undeclared family: {unknown}"

    def test_random_chance_floors_are_known_benchmarks(self, bundle):
        unknown = set(bundle.random_chance_floors()) - _range_names(bundle)
        assert unknown == set(), f"floors for unknown benchmarks: {sorted(unknown)}"

    def test_centroid_fingerprint_matches_training_queries(self, bundle):
        """The bundle's centroids were generated from its training queries."""
        meta = bundle.centroids["metadata"]
        expected = benchmark_fingerprint(sorted(_training_query_names(bundle)))
        assert meta.get("benchmark_fingerprint") == expected
        assert meta.get("benchmark_count") == len(_training_query_names(bundle))

    def test_registry_from_bundle_carries_the_taxonomy(self, bundle):
        registry = BenchmarkRegistry.from_bundle(bundle)
        assert registry.names == bundle.benchmark_names
        normalizer = registry.get_normalizer()
        for name, weight in bundle.benchmark_weights().items():
            assert normalizer.get_weight(name) == weight
        assert registry.random_chance_floors() == bundle.random_chance_floors()


class TestStarterTablesAreTheStarterBundle:
    """The backwards-compatible module tables are the packaged starter catalog."""

    def test_benchmark_weight_keys_match_normalization_keys(self):
        assert set(BENCHMARK_WEIGHTS) == set(NORMALIZATION_RANGES)

    def test_standard_benchmark_names_match_normalization_keys(self):
        standard_names = [b.name for b in STANDARD_BENCHMARKS]
        assert len(standard_names) == len(set(standard_names)), (
            "duplicate STANDARD_BENCHMARKS names"
        )
        assert set(standard_names) == set(NORMALIZATION_RANGES)
        assert standard_names == starter_bundle().benchmark_names

    def test_every_standard_benchmark_is_a_category(self):
        missing = {b.name for b in STANDARD_BENCHMARKS} - set(BENCHMARK_CATEGORIES)
        assert missing == set(), f"standard benchmarks missing a category: {sorted(missing)}"

    def test_random_chance_floors_are_the_starter_floors(self):
        assert RANDOM_CHANCE_FLOORS == starter_bundle().random_chance_floors()
        assert set(RANDOM_CHANCE_FLOORS) <= set(NORMALIZATION_RANGES)

    def test_standard_benchmark_ranges_come_from_normalization_table(self):
        """STANDARD_BENCHMARKS must reuse NORMALIZATION_RANGES, not carry copies."""
        for b in STANDARD_BENCHMARKS:
            r = NORMALIZATION_RANGES[b.name]
            assert (b.normalization.min_score, b.normalization.max_score) == (
                r.min_score,
                r.max_score,
            ), f"{b.name} range diverged from NORMALIZATION_RANGES"
