"""Tests for the embedding classifier.

These tests use a mock embedding provider to avoid downloading
the real sentence-transformers model in CI.
"""

import numpy as np

from tryaii_dre.centroids.loader import CentroidLoader
from tryaii_dre.classifiers.embedding import EmbeddingClassifier, _cosine_similarity
from tryaii_dre.config import TryaiiDreConfig
from tryaii_dre.embeddings.base import BaseEmbeddingProvider


class MockEmbeddingProvider(BaseEmbeddingProvider):
    """Mock embedding provider for testing."""

    def __init__(self, dim: int = 64):
        self._dim = dim
        self._call_count = 0

    def embed(self, text: str) -> np.ndarray:
        self._call_count += 1
        # Generate a deterministic vector based on the text
        rng = np.random.RandomState(hash(text) % 2**31)
        vec = rng.randn(self._dim).astype(np.float32)
        return vec / np.linalg.norm(vec)

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [self.embed(t) for t in texts]

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return "mock-model"


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = np.array([1.0, 0.0, 0.0])
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        a = np.array([1.0, 0.0, 0.0])
        b = np.array([0.0, 1.0, 0.0])
        assert abs(_cosine_similarity(a, b)) < 1e-6

    def test_opposite_vectors(self):
        a = np.array([1.0, 0.0, 0.0])
        b = np.array([-1.0, 0.0, 0.0])
        assert abs(_cosine_similarity(a, b) - (-1.0)) < 1e-6

    def test_zero_vector(self):
        a = np.array([1.0, 0.0])
        b = np.array([0.0, 0.0])
        assert _cosine_similarity(a, b) == 0.0


class TestEmbeddingClassifier:
    def setup_method(self):
        self.provider = MockEmbeddingProvider(dim=64)
        self.config = TryaiiDreConfig(embedding_model="mock-model")

        # Create a mock centroid loader that returns pre-computed centroids
        self.mock_centroids = {}
        rng = np.random.RandomState(42)
        for bench in ["MMLU", "HumanEval", "SWE-bench", "GSM8K", "MT-Bench",
                       "Chatbot Arena (LMSys)", "HellaSwag", "TruthfulQA",
                       "ARC", "DROP", "SuperGLUE", "LiveBench"]:
            vec = rng.randn(64).astype(np.float32)
            self.mock_centroids[bench] = vec / np.linalg.norm(vec)

    def _make_classifier(self) -> EmbeddingClassifier:
        loader = CentroidLoader(self.config, self.provider)
        # Inject mock centroids directly
        loader._centroids = self.mock_centroids
        return EmbeddingClassifier(self.provider, loader, self.config)

    def test_classify_returns_benchmark_scores(self):
        classifier = self._make_classifier()
        result = classifier.classify("Write a sorting algorithm")

        assert len(result.benchmark_scores) == len(self.mock_centroids)
        for name, score in result.benchmark_scores.items():
            assert 0.0 <= score <= 1.0 or score >= 0.0  # Clamped to >= 0

    def test_classify_sets_category(self):
        classifier = self._make_classifier()
        result = classifier.classify("Explain quantum physics")

        assert result.broad_category != ""
        assert result.classifier_used == "embedding"

    def test_caching_works(self):
        classifier = self._make_classifier()

        classifier.classify("Same prompt")
        call_count_after_first = self.provider._call_count

        result2 = classifier.classify("Same prompt")

        # Second call should use cache (no new embedding calls)
        assert self.provider._call_count == call_count_after_first
        assert result2.cache_hit is True

    def test_different_prompts_different_results(self):
        classifier = self._make_classifier()

        result1 = classifier.classify("Write Python code")
        result2 = classifier.classify("Tell me a bedtime story")

        # Different prompts should produce different scores (top-benchmark
        # ordering isn't guaranteed to differ with the mock provider).
        assert result1.benchmark_scores != result2.benchmark_scores
