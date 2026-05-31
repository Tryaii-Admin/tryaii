"""Tests for centroid generation."""

import tempfile
from pathlib import Path

import numpy as np

from tryaii_dre.centroids.generator import CentroidGenerator
from tryaii_dre.embeddings.base import BaseEmbeddingProvider


class MockProvider(BaseEmbeddingProvider):
    """Deterministic mock for testing centroid generation."""

    def embed(self, text: str) -> np.ndarray:
        rng = np.random.RandomState(hash(text) % 2**31)
        vec = rng.randn(32).astype(np.float32)
        return vec / np.linalg.norm(vec)

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [self.embed(t) for t in texts]

    @property
    def dimension(self) -> int:
        return 32

    @property
    def model_name(self) -> str:
        return "mock-32d"


class TestCentroidGenerator:
    def setup_method(self):
        self.provider = MockProvider()
        self.generator = CentroidGenerator(self.provider)

    def test_generate_from_queries(self):
        queries = {
            "TestBench1": ["query a", "query b", "query c"],
            "TestBench2": ["hello world", "foo bar"],
        }
        centroids = self.generator.generate(queries, show_progress=False)

        assert len(centroids) == 2
        assert "TestBench1" in centroids
        assert "TestBench2" in centroids
        assert centroids["TestBench1"].shape == (32,)

    def test_centroids_are_normalized(self):
        queries = {"Bench": ["a", "b", "c"]}
        centroids = self.generator.generate(queries, show_progress=False)
        norm = np.linalg.norm(centroids["Bench"])
        assert abs(norm - 1.0) < 1e-5

    def test_generate_single_custom(self):
        centroid = self.generator.generate_from_custom(
            "CustomBench", ["q1", "q2", "q3"]
        )
        assert centroid.shape == (32,)
        assert abs(np.linalg.norm(centroid) - 1.0) < 1e-5

    def test_save_and_load(self):
        queries = {"Bench1": ["a", "b"], "Bench2": ["c", "d"]}
        centroids = self.generator.generate(queries, show_progress=False)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name

        self.generator.save(centroids, path)

        loaded, metadata = CentroidGenerator.load(path)

        assert len(loaded) == 2
        assert metadata["model"] == "mock-32d"
        assert metadata["dimension"] == 32
        np.testing.assert_array_almost_equal(
            centroids["Bench1"], loaded["Bench1"], decimal=5
        )

        Path(path).unlink()

    def test_generate_default_queries(self):
        centroids = self.generator.generate(show_progress=False)
        # Should load from bundled training_queries.json
        assert len(centroids) >= 10  # 12 standard benchmarks
        assert "MMLU" in centroids
        assert "HumanEval" in centroids
