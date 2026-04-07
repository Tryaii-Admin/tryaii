"""Tests for the main Router class.

Includes both keyword-only tests (fast, deterministic) and embedding-based
tests that exercise the default Router() path advertised in the README.
"""

import numpy as np
import pytest

from tryaii_dre import Priorities, Router
from tryaii_dre.config import TryaiiDreConfig


class TestRouterKeywordOnly:
    """Test routing with keyword classifier (no embedding model needed)."""

    def setup_method(self):
        # Use keyword-only mode for fast, deterministic tests
        config = TryaiiDreConfig(classifier="keyword")
        self.router = Router(config=config)

    def test_route_returns_result(self):
        result = self.router.route("Write a Python function")
        assert result.best_model != ""
        assert len(result.scores) > 0

    def test_route_code_prompt(self):
        result = self.router.route("Implement a binary search algorithm in Python")
        assert result.classification.broad_category == "TECHNICAL"
        assert result.best_model != ""

    def test_route_creative_prompt(self):
        result = self.router.route("Write a short poem about the stars")
        assert result.classification.broad_category == "CREATIVE"

    def test_route_with_priorities(self):
        result_quality = self.router.route(
            "Debug this code",
            priorities=Priorities(quality=5, cost=1, speed=1),
        )
        result_budget = self.router.route(
            "Debug this code",
            priorities=Priorities(quality=1, cost=5, speed=3),
        )
        # Different priorities should (usually) produce different rankings
        # At minimum, scores should differ
        assert result_quality.scores[0].final_score != result_budget.scores[0].final_score or \
               result_quality.best_model != result_budget.best_model

    def test_route_top_k(self):
        result = self.router.route("Test prompt", top_k=3)
        assert len(result.scores) == 3

    def test_route_filter_provider(self):
        result = self.router.route(
            "Write code",
            filter_provider="Anthropic",
        )
        for score in result.scores:
            model = self.router.models.get_model(score.model_id)
            assert model.provider == "Anthropic"

    def test_route_filter_max_cost(self):
        result = self.router.route(
            "Write code",
            filter_max_cost=0.001,
        )
        for score in result.scores:
            model = self.router.models.get_model(score.model_id)
            assert model.pricing.input_per_1k <= 0.001

    def test_route_keyword_only_method(self):
        result = self.router.route_keyword_only("Calculate compound interest")
        assert result.classification.broad_category in ("TECHNICAL", "BUSINESS")
        assert result.best_model != ""

    def test_route_result_properties(self):
        result = self.router.route("Hello")
        assert isinstance(result.top_k, list)
        assert isinstance(result.best_score, float)
        assert isinstance(result.best_reasoning, str)

    def test_route_result_repr(self):
        result = self.router.route("Test")
        repr_str = repr(result)
        assert "RouteResult" in repr_str
        assert result.best_model in repr_str


class TestRouterCustomRegistry:
    def test_add_model_shortcut(self):
        config = TryaiiDreConfig(classifier="keyword")
        router = Router(config=config)

        router.add_model(
            "custom-model",
            provider="CustomProvider",
            benchmarks={"HumanEval": 95.0, "MMLU": 90.0},
            pricing=(0.001, 0.004),
            latency="fast",
        )

        assert "custom-model" in router.models

    def test_route_with_no_matching_models(self):
        config = TryaiiDreConfig(classifier="keyword")
        router = Router(config=config)

        result = router.route(
            "Write code",
            filter_provider="NonexistentProvider",
        )
        assert result.best_model == ""
        assert len(result.scores) == 0


class TestRouterEmbedding:
    """Test the default embedding-based routing path (the advertised README flow)."""

    @pytest.fixture(autouse=True)
    def _router(self, monkeypatch):
        import sentence_transformers

        class FakeSentenceTransformer:
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

        monkeypatch.setattr(
            sentence_transformers,
            "SentenceTransformer",
            FakeSentenceTransformer,
        )

        # Default Router() -- uses local sentence-transformers embeddings
        self.router = Router()

    def test_default_route_returns_result(self):
        """Smoke test: the README quickstart path must work out of the box."""
        result = self.router.route("Write a Python function to merge sorted arrays")
        assert result.best_model != ""
        assert len(result.scores) > 0
        assert result.classification is not None

    def test_default_route_with_priorities(self):
        result = self.router.route(
            "Explain quantum entanglement simply",
            priorities=Priorities(quality=5, cost=1, speed=2),
        )
        assert result.best_model != ""

    def test_embedding_classification_has_confidence(self):
        result = self.router.route("Debug this React component")
        assert result.classification is not None
        assert result.classification.confidence > 0
