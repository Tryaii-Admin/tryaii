"""
Neural embedding classifier.

Classifies prompts by computing cosine similarity between the prompt's
embedding vector and pre-computed benchmark centroids. This gives a
semantic understanding of "what kind of task" a prompt represents.
"""

from __future__ import annotations

import copy
import hashlib
import time
from typing import Optional

import numpy as np

from tryaii_dre.cache.lru import LRUCache
from tryaii_dre.centroids.generator import benchmark_fingerprint
from tryaii_dre.centroids.loader import CentroidLoader
from tryaii_dre.classifiers.base import BaseClassifier, ClassificationResult
from tryaii_dre.config import TryaiiDreConfig
from tryaii_dre.embeddings.base import BaseEmbeddingProvider

# Benchmark -> broad category mapping for display purposes
BENCHMARK_CATEGORIES: dict[str, tuple[str, str]] = {
    "MMLU": ("EDUCATIONAL", "ACADEMIC_INSTRUCTION"),
    "HellaSwag": ("CONVERSATIONAL", "PERSONAL_ADVICE"),
    "HumanEval": ("TECHNICAL", "CODE_TECHNICAL"),
    "SWE-bench": ("TECHNICAL", "CODE_TECHNICAL"),
    "TruthfulQA": ("CONVERSATIONAL", "PERSONAL_ADVICE"),
    "ARC": ("EDUCATIONAL", "ACADEMIC_INSTRUCTION"),
    "GSM8K": ("TECHNICAL", "MATHEMATICAL_SCIENTIFIC"),
    "DROP": ("TECHNICAL", "MATHEMATICAL_SCIENTIFIC"),
    "SuperGLUE": ("BUSINESS", "PROFESSIONAL_COMMUNICATION"),
    "Chatbot Arena (LMSys)": ("CONVERSATIONAL", "PERSONAL_ADVICE"),
    "MT-Bench": ("CREATIVE", "WRITING_LITERARY"),
    "LiveBench": ("TECHNICAL", "CODE_TECHNICAL"),
}


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors."""
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    # Treat zero or non-finite norms (NaN/inf) as zero similarity.
    if not np.isfinite(norm_a) or not np.isfinite(norm_b) or norm_a == 0 or norm_b == 0:
        return 0.0
    sim = float(dot / (norm_a * norm_b))
    if not np.isfinite(sim):
        return 0.0
    return sim


class EmbeddingClassifier(BaseClassifier):
    """
    Semantic classifier using embedding cosine similarity.

    Flow:
        1. Embed the user prompt using the configured embedding provider
        2. Compute cosine similarity against each benchmark centroid
        3. Return similarity scores as the classification result

    Includes LRU caching for both embeddings and full classification results.
    """

    def __init__(
        self,
        embedding_provider: BaseEmbeddingProvider,
        centroid_loader: CentroidLoader,
        config: Optional[TryaiiDreConfig] = None,
    ):
        self._provider = embedding_provider
        self._centroid_loader = centroid_loader
        self._config = config or TryaiiDreConfig()

        # Caches
        self._embedding_cache = LRUCache[np.ndarray](
            max_size=self._config.cache.embedding_cache_size,
            ttl_seconds=self._config.cache.ttl_seconds,
        )
        self._classification_cache = LRUCache[ClassificationResult](
            max_size=self._config.cache.classification_cache_size,
            ttl_seconds=self._config.cache.ttl_seconds,
        )

        self._ready = False

    def classify(self, prompt: str) -> ClassificationResult:
        """Classify a prompt using embedding similarity to benchmark centroids."""
        start = time.time()

        # Ensure centroids are loaded
        centroids = self._centroid_loader.get_centroids()

        # Build a cache key that is scoped to the embedding model, dimension,
        # and the benchmark-set fingerprint -- not just the prompt -- so cached
        # results don't leak across models or benchmark sets.
        cache_key = self._cache_key(prompt, centroids)
        cached = self._classification_cache.get(cache_key)
        if cached is not None:
            result = copy.copy(cached)
            result.cache_hit = True
            result.processing_time_ms = (time.time() - start) * 1000
            return result

        # Empty centroid map: return a well-defined zero-confidence result
        # rather than crashing on max([]) or fabricating a label.
        if not centroids:
            result = ClassificationResult(
                benchmark_scores={},
                broad_category="",
                subcategory="",
                confidence=0.0,
                classifier_used="embedding",
                cache_hit=False,
                processing_time_ms=(time.time() - start) * 1000,
            )
            self._classification_cache.set(cache_key, result)
            self._ready = True
            return result

        # Get prompt embedding (with caching)
        embedding = self._get_embedding(prompt)

        # Calculate cosine similarity against each benchmark centroid
        benchmark_scores: dict[str, float] = {}
        for benchmark_name, centroid in centroids.items():
            similarity = _cosine_similarity(embedding, centroid)
            # Clamp to [0, 1] -- negative similarities are not meaningful here
            benchmark_scores[benchmark_name] = max(0.0, similarity)

        # Determine top category from highest-scoring benchmark.
        # Break ties deterministically by (score desc, benchmark_name asc),
        # matching ClassificationResult.top_benchmarks ordering.
        top_benchmark = min(
            benchmark_scores,
            key=lambda name: (-benchmark_scores[name], name),
        )
        top_score = benchmark_scores[top_benchmark]

        broad_cat, sub_cat = BENCHMARK_CATEGORIES.get(
            top_benchmark, ("TECHNICAL", "CODE_TECHNICAL")
        )

        result = ClassificationResult(
            benchmark_scores=benchmark_scores,
            broad_category=broad_cat,
            subcategory=sub_cat,
            confidence=top_score,
            classifier_used="embedding",
            cache_hit=False,
            processing_time_ms=(time.time() - start) * 1000,
        )

        # Cache the result
        self._classification_cache.set(cache_key, result)
        self._ready = True

        return result

    def _get_embedding(self, text: str) -> np.ndarray:
        """Get embedding with caching."""
        # Embedding cache key is scoped to the embedding model + dimension so
        # vectors from different models never collide on the same prompt hash.
        cache_key = self._embedding_cache_key(text)
        cached = self._embedding_cache.get(cache_key)
        if cached is not None:
            return cached

        embedding = self._provider.embed(text)
        self._embedding_cache.set(cache_key, embedding)
        return embedding

    def is_ready(self) -> bool:
        return True  # Lazy initialization handles readiness

    def _embedding_cache_key(self, text: str) -> str:
        """Cache key scoped to the embedding model name + dimension."""
        scope = f"{self._provider.model_name}:{self._provider.dimension}"
        return self._hash_prompt(f"{scope}:{text}")

    def _cache_key(self, prompt: str, centroids: dict[str, np.ndarray]) -> str:
        """
        Classification cache key scoped to the embedding model name, the
        embedding dimension, and the benchmark-set fingerprint.
        """
        fingerprint = benchmark_fingerprint(centroids.keys())
        scope = f"{self._provider.model_name}:{self._provider.dimension}:{fingerprint}"
        return self._hash_prompt(f"{scope}:{prompt}")

    @staticmethod
    def _hash_prompt(prompt: str) -> str:
        """Create a cache key from a prompt."""
        return hashlib.md5(prompt.encode()).hexdigest()
