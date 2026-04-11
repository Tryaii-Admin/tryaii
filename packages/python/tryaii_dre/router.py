"""
Main Router -- the primary public API for TryAii-DRE.

Usage:
    from tryaii_dre import Router

    router = Router()
    result = router.route("Write a Python function to merge sorted arrays")
    print(result.best_model)     # "gpt-5.2"
    print(result.scores[:3])     # Top 3 with scores and reasoning
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from tryaii_dre.benchmarks.registry import BenchmarkRegistry
from tryaii_dre.centroids.loader import CentroidLoader
from tryaii_dre.classifiers.base import MAX_PROMPT_LENGTH, ClassificationResult
from tryaii_dre.classifiers.embedding import EmbeddingClassifier
from tryaii_dre.config import TryaiiDreConfig
from tryaii_dre.embeddings.local import LocalEmbeddingProvider
from tryaii_dre.registry.models import ModelRegistry
from tryaii_dre.scoring.engine import ModelScore, ScoringEngine
from tryaii_dre.scoring.priorities import DEFAULT_PRIORITIES, Priorities

logger = logging.getLogger("tryaii_dre")


@dataclass
class RouteResult:
    """
    Result of routing a prompt.

    Contains the recommended model, all scored models, and classification details.
    """

    # The top recommended model ID
    best_model: str

    # All scored models (sorted by score descending)
    scores: list[ModelScore] = field(default_factory=list)

    # Classification details (what kind of task was detected)
    classification: Optional[ClassificationResult] = None

    # Priorities used for this routing decision
    priorities: Priorities = field(default_factory=lambda: DEFAULT_PRIORITIES)

    @property
    def top_k(self) -> list[str]:
        """List of model IDs in ranked order."""
        return [s.model_id for s in self.scores]

    @property
    def best_score(self) -> float:
        """Score of the top model."""
        return self.scores[0].final_score if self.scores else 0.0

    @property
    def best_reasoning(self) -> str:
        """Reasoning for why the top model was chosen."""
        return self.scores[0].reasoning if self.scores else ""

    def __repr__(self) -> str:
        top3 = ", ".join(f"{s.model_id}({s.final_score:.2f})" for s in self.scores[:3])
        return f"RouteResult(best={self.best_model}, top3=[{top3}])"


class Router:
    """
    Semantic AI model router.

    Analyzes user prompts using embeddings, matches them against benchmark
    centroids, and recommends the best AI model based on benchmark performance,
    pricing, latency, and user priorities.

    Args:
        config: Configuration overrides. If None, uses defaults.
        registry: Model registry. If None, loads the default 35+ models.
        benchmark_registry: Benchmark definitions. If None, uses standard 12.
        embedding_provider: Custom embedding provider. If None, uses local
                           sentence-transformers (all-MiniLM-L6-v2).
    """

    def __init__(
        self,
        config: Optional[TryaiiDreConfig] = None,
        registry: Optional[ModelRegistry] = None,
        benchmark_registry: Optional[BenchmarkRegistry] = None,
        embedding_provider=None,
    ):
        self._config = config or TryaiiDreConfig()

        # Model registry
        self._registry = registry or ModelRegistry.default()

        # Benchmark registry
        self._benchmark_registry = benchmark_registry or BenchmarkRegistry.default()

        # Scoring engine with normalizer from benchmark registry
        normalizer = self._benchmark_registry.get_normalizer()
        self._scoring_engine = ScoringEngine(normalizer=normalizer)

        # Embedding provider (lazy -- only initialized when needed)
        self._embedding_provider = embedding_provider
        self._classifier: Optional[EmbeddingClassifier] = None

    def _ensure_classifier(self) -> EmbeddingClassifier:
        """Lazy-initialize the embedding classifier on first use."""
        if self._classifier is not None:
            return self._classifier

        # Initialize embedding provider
        if self._embedding_provider is None:
            self._embedding_provider = LocalEmbeddingProvider(
                model_name=self._config.embedding_model
            )

        # Initialize centroid loader
        centroid_loader = CentroidLoader(
            config=self._config,
            embedding_provider=self._embedding_provider,
        )

        self._classifier = EmbeddingClassifier(
            embedding_provider=self._embedding_provider,
            centroid_loader=centroid_loader,
            config=self._config,
        )

        return self._classifier

    def route(
        self,
        prompt: str,
        priorities: Optional[Priorities] = None,
        top_k: int = 5,
        filter_provider: Optional[str] = None,
        filter_capability: Optional[str] = None,
        filter_max_cost: Optional[float] = None,
    ) -> RouteResult:
        """
        Route a prompt to the best AI model.

        Args:
            prompt: The user's input text to classify and route.
            priorities: Quality/cost/speed priorities. Defaults to balanced.
            top_k: Number of top models to return.
            filter_provider: Only consider models from this provider.
            filter_capability: Only consider models with this capability.
            filter_max_cost: Only consider models cheaper than this (input $/1k tokens).

        Returns:
            RouteResult with the best model and full scoring breakdown.
        """
        if not prompt or not isinstance(prompt, str):
            raise ValueError("prompt must be a non-empty string")
        if len(prompt) > MAX_PROMPT_LENGTH:
            prompt = prompt[:MAX_PROMPT_LENGTH]

        priorities = priorities or DEFAULT_PRIORITIES

        # 1. Classify the prompt
        classifier = self._ensure_classifier()
        classification = classifier.classify(prompt)

        # 2. Get available models (with optional filters)
        models = self._registry.all_models
        if filter_provider:
            models = [m for m in models if m.provider.lower() == filter_provider.lower()]
        if filter_capability:
            models = [m for m in models if filter_capability in m.capabilities]
        if filter_max_cost is not None:
            models = [
                m for m in models
                if m.pricing and m.pricing.input_per_1k <= filter_max_cost
            ]

        if not models:
            return RouteResult(
                best_model="",
                scores=[],
                classification=classification,
                priorities=priorities,
            )

        # 3. Score and rank models
        scores = self._scoring_engine.score_models(
            models=models,
            benchmark_similarities=classification.benchmark_scores,
            priorities=priorities,
            top_k=top_k,
        )

        best = scores[0].model_id if scores else ""

        logger.info("Route completed best=%s category=%s confidence=%.3f top_k=%d",
                     best, classification.broad_category, classification.confidence, len(scores))

        return RouteResult(
            best_model=best,
            scores=scores,
            classification=classification,
            priorities=priorities,
        )

    def add_model(self, *args, **kwargs):
        """Shortcut to add a model to the registry. See ModelRegistry.add()."""
        return self._registry.add(*args, **kwargs)

    def add_benchmark(
        self,
        name: str,
        queries: list[str],
        description: str = "",
        min_score: float = 0,
        max_score: float = 100,
    ) -> None:
        """
        Add a custom benchmark to the routing system.

        Args:
            name: Benchmark name (e.g., "CustomerSupportQA").
            queries: Representative prompts for this benchmark (10-20 recommended).
            description: Human-readable description.
            min_score: Minimum score for normalization.
            max_score: Maximum score for normalization.
        """
        from tryaii_dre.benchmarks.registry import BenchmarkDefinition
        from tryaii_dre.scoring.benchmarks import NormalizationRange

        # Register in benchmark registry
        benchmark = BenchmarkDefinition(
            name=name,
            description=description,
            training_queries=queries,
            normalization=NormalizationRange(min_score, max_score, description),
        )
        self._benchmark_registry.register(benchmark)

        # Update scoring engine normalizer
        normalizer = self._benchmark_registry.get_normalizer()
        self._scoring_engine = ScoringEngine(normalizer=normalizer)

        # Generate centroid if classifier is initialized
        if self._classifier is not None and self._embedding_provider is not None:
            centroid_loader = CentroidLoader(
                config=self._config,
                embedding_provider=self._embedding_provider,
            )
            centroid_loader.add_benchmark_centroid(name, queries)

        logger.info(f"Added custom benchmark: {name} ({len(queries)} queries)")

    @property
    def models(self) -> ModelRegistry:
        """Access the model registry."""
        return self._registry

    @property
    def benchmarks(self) -> BenchmarkRegistry:
        """Access the benchmark registry."""
        return self._benchmark_registry

    @property
    def config(self) -> TryaiiDreConfig:
        """Access the configuration."""
        return self._config
