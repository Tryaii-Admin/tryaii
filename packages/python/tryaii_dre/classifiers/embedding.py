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

# Logistic steepness for intrinsic difficulty. Only affects the spread of the
# reported [0,1] value; ordering (which drives batch-normalized allocation) is
# scale-invariant. Must stay in sync with the Node SDK (DIFFICULTY_SCALE).
DIFFICULTY_SCALE = 10.0

# Canonical EASY exemplars (atomic / single-step / lookup), spanning many domains
# so the difficulty axis encodes COMPLEXITY, not topic. Paired by domain with
# HARD_EXEMPLARS. Must stay in sync with the Node SDK (classifiers/embedding.ts).
EASY_EXEMPLARS: list[str] = [
    "What is the capital of Japan?",
    "Which planet is closest to the sun?",
    "How many continents are there on Earth?",
    "How many days are in a week?",
    "What is 7 times 8?",
    "What is 25 percent of 80?",
    "Round 3.14159 to two decimal places.",
    "What is the square root of 144?",
    "What does the 'len()' function do in Python?",
    "How do I print 'Hello, World!' in JavaScript?",
    "Which command stages all changes in Git?",
    "How do I declare a constant variable in JavaScript?",
    "If all cats are animals and Tom is a cat, is Tom an animal?",
    "Does 'P implies Q' mean Q is true whenever P is true?",
    "If A is taller than B, who is shorter?",
    "Is the statement 'it is raining and it is not raining' true or false?",
    "Translate 'good morning' into German.",
    "What is the plural of 'cactus'?",
    "Correct the spelling: 'recieve'.",
    "How do you say 'thank you' in Japanese?",
    "What does ROI stand for in business?",
    "What is the formula to calculate gross profit?",
    "What does the acronym KPI mean?",
    "Convert 250 US dollars to euros at a rate of 1.08.",
]

# Canonical HARD exemplars (multi-step / open-ended / design / proof).
HARD_EXEMPLARS: list[str] = [
    "Explain how a refrigerator keeps food cold using the principles of thermodynamics.",
    "Assess how the printing press reshaped literacy, religion, and politics across early modern Europe.",
    "Explain why the sky is blue at noon but red at sunset, accounting for light scattering.",
    "Compare the long-term societal trade-offs of nuclear, solar, and coal energy for a national grid.",
    "Prove that the square root of 2 is irrational and justify each step rigorously.",
    "Derive a closed-form expression for the sum of the first n cubes and prove it by induction.",
    "Evaluate the integral of e to the negative x squared from negative infinity to infinity and explain the method used.",
    "Determine the expected number of draws to collect all n distinct coupons and analyze its asymptotic growth.",
    "Design a horizontally scalable, exactly-once message queue and discuss its consistency trade-offs.",
    "Implement a lock-free concurrent hash map and prove it is linearizable.",
    "Architect a multi-region database with automatic failover and explain how you resolve write conflicts.",
    "Refactor a tightly coupled monolith into event-driven microservices while preserving transactional integrity.",
    "Determine whether this argument is valid and name any fallacy it commits, justifying each step.",
    "Prove that the given set of logical premises is inconsistent using natural deduction.",
    "Solve the knights-and-knaves puzzle and explain the chain of inferences leading to each identity.",
    "Resolve the apparent paradox in this self-referential statement and explain why naive interpretations fail.",
    "Translate this poem into French while preserving its rhyme scheme and meter.",
    "Rewrite this technical manual for a sixth-grade reading level without losing accuracy.",
    "Write a persuasive essay arguing both sides of a dilemma, then synthesize a conclusion.",
    "Compare how three languages encode politeness and what that reveals about each culture.",
    "Build a three-year financial model with scenario analysis and justify every assumption.",
    "Design a churn-prediction pipeline from raw event logs and defend your feature choices.",
    "Develop a market-entry strategy for a new region and quantify the risk-adjusted payback.",
    "Construct a data-governance policy covering retention, access control, and regulatory compliance across jurisdictions.",
]


def _normalize(v: np.ndarray) -> np.ndarray:
    """Unit-normalize a vector; return it unchanged on a zero/non-finite norm."""
    norm = np.linalg.norm(v)
    if not np.isfinite(norm) or norm == 0:
        return v
    return v / norm


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
        self._easy_centroid: Optional[np.ndarray] = None
        self._hard_centroid: Optional[np.ndarray] = None

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
        self._ensure_difficulty_centroids()

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
            difficulty=self._intrinsic_difficulty(embedding),
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

    def _ensure_difficulty_centroids(self) -> None:
        """Build the easy/hard difficulty centroids once from the bundled exemplars."""
        if self._easy_centroid is not None and self._hard_centroid is not None:
            return
        easy = self._provider.embed_batch(EASY_EXEMPLARS)
        hard = self._provider.embed_batch(HARD_EXEMPLARS)
        self._easy_centroid = _normalize(np.mean(np.stack(easy), axis=0))
        self._hard_centroid = _normalize(np.mean(np.stack(hard), axis=0))

    def _intrinsic_difficulty(self, embedding: np.ndarray) -> float:
        """Intrinsic difficulty in [0, 1]: how much closer the prompt sits to the
        HARD exemplars than the EASY ones, squashed through a logistic. Returns 0
        when the difficulty centroids haven't been built."""
        if self._easy_centroid is None or self._hard_centroid is None:
            return 0.0
        sim_hard = _cosine_similarity(embedding, self._hard_centroid)
        sim_easy = _cosine_similarity(embedding, self._easy_centroid)
        return float(1.0 / (1.0 + np.exp(-DIFFICULTY_SCALE * (sim_hard - sim_easy))))

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
