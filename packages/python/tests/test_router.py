"""Tests for the main Router class.

The router is embedding-only. Tests use a deterministic FakeSentenceTransformer
so they run instantly in CI without downloading model weights.
"""

import numpy as np
import pytest

from tryaii_dre import Priorities, Router
from tryaii_dre.config import TryaiiDreConfig


class FakeSentenceTransformer:
    """Deterministic stand-in for sentence_transformers.SentenceTransformer."""

    def __init__(self, model_name: str, device=None):
        self.model_name = model_name
        self.device = device

    def encode(
        self,
        texts,
        convert_to_numpy=True,
        normalize_embeddings=False,
        batch_size=32,
    ):
        if isinstance(texts, str):
            texts = [texts]

        vectors = []
        for text in texts:
            seed = abs(hash(text)) % (2**32)
            rng = np.random.RandomState(seed)
            vector = rng.randn(384).astype(np.float32)
            if normalize_embeddings:
                vector /= np.linalg.norm(vector)
            vectors.append(vector)

        return np.stack(vectors)


@pytest.fixture
def router(monkeypatch):
    """A Router whose embedding provider is backed by FakeSentenceTransformer."""
    import sentence_transformers

    monkeypatch.setattr(
        sentence_transformers,
        "SentenceTransformer",
        FakeSentenceTransformer,
    )
    return Router()


class TestRouter:
    """Default embedding-based routing path (the advertised README flow)."""

    def test_route_returns_result(self, router):
        result = router.route("Write a Python function to merge sorted arrays")
        assert result.best_model != ""
        assert len(result.scores) > 0
        assert result.classification is not None

    def test_classifier_used_is_embedding(self, router):
        result = router.route("Write a Python function")
        assert result.classification.classifier_used == "embedding"

    def test_route_with_priorities(self, router):
        result_quality = router.route(
            "Debug this code",
            priorities=Priorities(quality=5, cost=1, speed=1),
        )
        result_budget = router.route(
            "Debug this code",
            priorities=Priorities(quality=1, cost=5, speed=3),
        )
        # Different priorities should produce different rankings or scores
        assert (
            result_quality.scores[0].final_score != result_budget.scores[0].final_score
            or result_quality.best_model != result_budget.best_model
        )

    def test_route_top_k(self, router):
        result = router.route("Test prompt", top_k=3)
        assert len(result.scores) == 3

    def test_route_filter_provider(self, router):
        result = router.route(
            "Write code",
            filter_provider="Anthropic",
        )
        for score in result.scores:
            model = router.models.get_model(score.model_id)
            assert model.provider == "Anthropic"

    def test_route_filter_max_cost(self, router):
        result = router.route(
            "Write code",
            filter_max_cost=0.001,
        )
        for score in result.scores:
            model = router.models.get_model(score.model_id)
            assert model.pricing.input_per_1k <= 0.001

    def test_route_result_properties(self, router):
        result = router.route("Hello")
        assert isinstance(result.top_k, list)
        assert isinstance(result.best_score, float)
        assert isinstance(result.best_reasoning, str)

    def test_route_result_repr(self, router):
        result = router.route("Test")
        repr_str = repr(result)
        assert "RouteResult" in repr_str
        assert result.best_model in repr_str

    def test_embedding_classification_has_confidence(self, router):
        result = router.route("Debug this React component")
        assert result.classification is not None
        assert result.classification.confidence > 0

    def test_default_route_with_priorities(self, router):
        result = router.route(
            "Explain quantum entanglement simply",
            priorities=Priorities(quality=5, cost=1, speed=2),
        )
        assert result.best_model != ""


class TestRouterCustomRegistry:
    def test_add_model_shortcut(self, router):
        router.add_model(
            "custom-model",
            provider="CustomProvider",
            benchmarks={"HumanEval": 95.0, "MMLU": 90.0},
            pricing=(0.001, 0.004),
            latency="fast",
        )

        assert "custom-model" in router.models

    def test_route_with_no_matching_models(self, router):
        result = router.route(
            "Write code",
            filter_provider="NonexistentProvider",
        )
        assert result.best_model == ""
        assert len(result.scores) == 0


class TestRouterConfigOverrides:
    def test_custom_config_is_honored(self, monkeypatch):
        import sentence_transformers

        monkeypatch.setattr(
            sentence_transformers,
            "SentenceTransformer",
            FakeSentenceTransformer,
        )

        config = TryaiiDreConfig(embedding_model="all-MiniLM-L6-v2")
        router = Router(config=config)
        result = router.route("Write a sorting algorithm")
        assert result.best_model != ""
        assert router.config.embedding_model == "all-MiniLM-L6-v2"
