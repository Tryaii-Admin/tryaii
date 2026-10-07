"""Integrity tests for the catalog bundles' centroids and training queries.

The centroids are the classifier's half of a catalog: unit-mean embeddings
(all-MiniLM-L6-v2, 384-dim) built from the bundle's training queries -- 16 in
the packaged starter catalog, 33 in the full one. A centroid file that
disagrees with the training queries -- or contains junk vectors -- silently
mis-classifies every prompt, so pin its structure here.
"""

from __future__ import annotations

import math

import pytest

from tryaii.catalog import starter_bundle
from tryaii.centroids.generator import benchmark_fingerprint

from ..conftest import load_full_bundle

_EXPECTED_COUNT = {"starter": 16, "full": 33}


@pytest.fixture(params=["starter", "full"])
def kind(request):
    return request.param


def _bundle(kind):
    return starter_bundle() if kind == "starter" else load_full_bundle()


def _centroids(kind) -> dict:
    return _bundle(kind).centroids


def _training(kind) -> dict:
    return _bundle(kind).training_queries


class TestBundledCentroids:
    def test_metadata_matches_default_embedding_model(self, kind):
        meta = _centroids(kind)["metadata"]
        assert meta["model"] == "all-MiniLM-L6-v2"
        assert meta["dimension"] == 384

    def test_metadata_benchmark_count_matches_payload(self, kind):
        data = _centroids(kind)
        assert (
            data["metadata"]["benchmark_count"]
            == len(data["centroids"])
            == _EXPECTED_COUNT[kind]
        )

    def test_fingerprint_matches_centroid_names(self, kind):
        data = _centroids(kind)
        expected = benchmark_fingerprint(data["centroids"].keys())
        assert data["metadata"]["benchmark_fingerprint"] == expected

    def test_every_centroid_has_correct_dimension(self, kind):
        data = _centroids(kind)
        wrong = {
            name: len(vec)
            for name, vec in data["centroids"].items()
            if len(vec) != data["metadata"]["dimension"]
        }
        assert wrong == {}

    def test_every_centroid_is_finite(self, kind):
        data = _centroids(kind)
        for name, vec in data["centroids"].items():
            assert all(math.isfinite(x) for x in vec), f"non-finite values in {name}"

    def test_every_centroid_is_unit_normalized(self, kind):
        data = _centroids(kind)
        for name, vec in data["centroids"].items():
            norm = math.sqrt(sum(x * x for x in vec))
            assert abs(norm - 1.0) < 1e-3, f"{name} norm={norm}"

    def test_centroids_are_distinct(self, kind):
        """No two benchmarks may share (near-)identical centroids -- that would
        make them indistinguishable to the classifier."""
        data = _centroids(kind)
        items = list(data["centroids"].items())
        for i, (name_a, a) in enumerate(items):
            for name_b, b in items[i + 1 :]:
                cos = sum(x * y for x, y in zip(a, b))
                assert cos < 0.999, (
                    f"{name_a} and {name_b} centroids nearly identical (cos={cos:.4f})"
                )


class TestTrainingQueries:
    def test_covers_the_bundle_benchmarks(self, kind):
        data = _training(kind)
        assert len(data["benchmarks"]) == _EXPECTED_COUNT[kind]
        assert set(data["benchmarks"]) == set(_bundle(kind).benchmark_names)

    def test_every_benchmark_has_query_text(self, kind):
        data = _training(kind)
        empty = [
            name
            for name, body in data["benchmarks"].items()
            if not [q for q in body.get("queries", []) if q and q.strip()]
        ]
        assert empty == [], f"benchmarks without training queries: {empty}"

    def test_queries_are_reasonably_sized_sets(self, kind):
        """Centroid quality needs a handful of queries per benchmark."""
        data = _training(kind)
        thin = {
            name: len(body["queries"])
            for name, body in data["benchmarks"].items()
            if len(body["queries"]) < 5
        }
        assert thin == {}, f"benchmarks with too few queries: {thin}"

    def test_queries_are_unique_within_each_benchmark(self, kind):
        data = _training(kind)
        for name, body in data["benchmarks"].items():
            qs = body["queries"]
            assert len(qs) == len(set(qs)), f"duplicate queries in {name}"
