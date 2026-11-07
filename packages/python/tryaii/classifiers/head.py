"""
Head classifier: the H4 router (default since H4).

The prompt's static embedding (:class:`~tryaii.embeddings.static_provider.
StaticEmbeddingProvider`) goes through a one-hidden-layer head trained on the
benchmark labels::

    logits = W1 relu(W0 e + b0) + b1
    p      = softmax(logits / T)        T = 2.0 (meta.json "temperature")

``p`` -- one probability per benchmark in ``classes.json`` -- is what the
scoring engine gets as ``benchmark_scores`` (in place of centroid cosine
similarities), restricted to the benchmarks of the loaded catalog: classes
whose benchmark the catalog does not have are dropped and the rest are
renormalised to sum to 1 (computed as the softmax of the kept logits / T,
which is the same thing). ``masked_classes`` on the result says how many were
dropped; ``confidence`` is the top kept probability. When the catalog has
none of the classes the head cannot route, and the Router falls back to
MiniLM (:func:`tryaii.classifiers.selection.load_h4_classifier`). Difficulty keeps the
easy/hard exemplar formula of :mod:`tryaii.classifiers.embedding`, with the
exemplars embedded by the same static model. No centroids are used.

The head runs in float64 (the weights are float32): a few hundred thousand
multiply-adds per prompt, and the same numbers on every CPU to well below the
1e-4 the router is checked to.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Collection, Iterable
from pathlib import Path
from typing import Optional

import numpy as np

from tryaii.cache.lru import LRUCache
from tryaii.catalog.bundle import CatalogBundle
from tryaii.classifiers.base import BaseClassifier, ClassificationResult
from tryaii.classifiers.embedding import (
    BENCHMARK_CATEGORIES,
    DIFFICULTY_SCALE,
    EASY_EXEMPLARS,
    FALLBACK_CATEGORY,
    HARD_EXEMPLARS,
    _cosine_similarity,
    _normalize,
)
from tryaii.config import TryaiiDreConfig
from tryaii.embeddings.static_provider import StaticEmbeddingProvider, load_safetensors


def softmax(logits: np.ndarray, temperature: float = 1.0) -> np.ndarray:
    """softmax(logits / temperature), max-shifted, float64."""
    x = np.asarray(logits, dtype=np.float64) / float(temperature)
    x = x - x.max(axis=-1, keepdims=True)
    p = np.exp(x)
    return p / p.sum(axis=-1, keepdims=True)


class RouterHead:
    """The classification head of a router payload (``head.safetensors``,
    ``classes.json``, ``meta.json``)."""

    def __init__(self, layers: list[tuple[np.ndarray, np.ndarray]], classes: list[str],
                 temperature: float):
        if not layers:
            raise ValueError("a head needs at least one layer")
        if len(classes) != layers[-1][0].shape[0] or len(set(classes)) != len(classes):
            raise ValueError("classes do not match the head's output size")
        self._layers = [(np.asarray(w, dtype=np.float64), np.asarray(b, dtype=np.float64))
                        for w, b in layers]
        self.classes = list(classes)
        self.temperature = float(temperature)
        self.input_dim = int(self._layers[0][0].shape[1])

    @classmethod
    def from_dir(cls, directory: Path) -> RouterHead:
        directory = Path(directory)
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        tensors = load_safetensors(directory / "head.safetensors")
        layers = []
        i = 0
        while f"W{i}" in tensors:
            layers.append((tensors[f"W{i}"], tensors[f"b{i}"]))
            i += 1
        classes = json.loads((directory / "classes.json").read_text(encoding="utf-8"))
        head = cls(layers, classes, float(meta["temperature"]))
        if head.input_dim != int(meta["dim"]) or len(classes) != int(meta["num_classes"]):
            raise ValueError(f"{directory}: head does not match meta.json")
        return head

    def logits(self, embedding: np.ndarray) -> np.ndarray:
        x = np.asarray(embedding, dtype=np.float64)
        last = len(self._layers) - 1
        for i, (w, b) in enumerate(self._layers):
            x = w @ x + b
            if i < last:
                x = np.maximum(x, 0.0)
        return x

    def probabilities(self, embedding: np.ndarray) -> np.ndarray:
        return softmax(self.logits(embedding), self.temperature)

    def scores(self, embedding: np.ndarray,
               keep: Optional[Collection[str]] = None) -> dict[str, float]:
        """``{benchmark: probability}`` for every class, or -- with ``keep`` --
        for the classes in ``keep`` only, renormalised to sum to 1 (the
        softmax of their logits / T). Empty when no class is kept."""
        if keep is None:
            p = self.probabilities(embedding)
            return {name: float(v) for name, v in zip(self.classes, p)}
        idx = [i for i, name in enumerate(self.classes) if name in keep]
        if not idx:
            return {}
        p = softmax(self.logits(embedding)[idx], self.temperature)
        return {self.classes[i]: float(v) for i, v in zip(idx, p)}


class HeadClassifier(BaseClassifier):
    """The H4 router as a classifier (see the module docstring).

    Args:
        provider: The static embedding provider (loaded on first use).
        head: The head; default: read from the provider's payload directory.
        config: Cache sizes / TTL.
        bundle: Catalog bundle whose category labels to use (else the starter
            catalog's table) and whose benchmarks the classes are masked to.
        catalog_benchmarks: The benchmark names to keep (default: the
            bundle's; no bundle and no names: every class is kept).
    """

    classifier_name = "head"

    def __init__(self, provider: StaticEmbeddingProvider, head: Optional[RouterHead] = None,
                 config: Optional[TryaiiDreConfig] = None,
                 bundle: Optional[CatalogBundle] = None,
                 categories: Optional[dict[str, tuple[str, str]]] = None,
                 catalog_benchmarks: Optional[Iterable[str]] = None):
        self._provider = provider
        if catalog_benchmarks is None and isinstance(bundle, CatalogBundle):
            catalog_benchmarks = bundle.benchmark_names
        self._catalog: Optional[frozenset[str]] = (
            frozenset(catalog_benchmarks) if catalog_benchmarks is not None else None)
        self._head = head
        self._config = config or TryaiiDreConfig()
        if categories is None:
            categories = (bundle.benchmark_categories() if isinstance(bundle, CatalogBundle)
                          else BENCHMARK_CATEGORIES)
        self._categories: dict[str, tuple[str, str]] = dict(categories)
        self._embedding_cache = LRUCache[np.ndarray](
            max_size=self._config.cache.embedding_cache_size,
            ttl_seconds=self._config.cache.ttl_seconds,
        )
        self._classification_cache = LRUCache[ClassificationResult](
            max_size=self._config.cache.classification_cache_size,
            ttl_seconds=self._config.cache.ttl_seconds,
        )
        self._easy_centroid: Optional[np.ndarray] = None
        self._hard_centroid: Optional[np.ndarray] = None

    # -- loading -----------------------------------------------------------

    def load(self) -> HeadClassifier:
        """Load (and verify) the payload now; raises when it is unavailable
        or broken. The Router calls this to decide on the fallback."""
        directory = self._provider.ensure_loaded()
        if self._head is None:
            self._head = RouterHead.from_dir(directory)
        if self._head.input_dim != self._provider.dimension:
            raise ValueError("head and embedding dimensions differ")
        return self

    @property
    def head(self) -> RouterHead:
        self.load()
        return self._head  # type: ignore[return-value]

    @property
    def kept_classes(self) -> list[str]:
        """The head's classes the catalog has (all of them without a
        catalog); loads the head."""
        classes = self.head.classes
        if self._catalog is None:
            return list(classes)
        return [c for c in classes if c in self._catalog]

    @property
    def masked_classes(self) -> int:
        """How many of the head's classes the catalog does not have."""
        return len(self.head.classes) - len(self.kept_classes)

    @property
    def provider(self) -> StaticEmbeddingProvider:
        return self._provider

    @property
    def model_id(self) -> str:
        return self._provider.model_name

    # -- classification ------------------------------------------------------

    def classify(self, prompt: str) -> ClassificationResult:
        start = time.time()
        key = self._hash(f"{self.model_id}:classify:{prompt}")
        cached = self._classification_cache.get(key)
        if cached is not None:
            result = copy.copy(cached)
            result.cache_hit = True
            result.processing_time_ms = (time.time() - start) * 1000
            return result

        head = self.head
        embedding = self._get_embedding(prompt)
        scores = head.scores(embedding, self._catalog)
        if not scores:
            raise ValueError("none of the H4 router's classes is in the catalog")
        top = min(scores, key=lambda name: (-scores[name], name))
        broad, sub = self._categories.get(top, FALLBACK_CATEGORY)
        self._ensure_difficulty_centroids()
        result = ClassificationResult(
            benchmark_scores=scores,
            broad_category=broad,
            subcategory=sub,
            confidence=scores[top],
            classifier_used=self.classifier_name,
            cache_hit=False,
            processing_time_ms=(time.time() - start) * 1000,
            difficulty=self._intrinsic_difficulty(embedding),
            masked_classes=len(head.classes) - len(scores),
        )
        self._classification_cache.set(key, result)
        return result

    def _get_embedding(self, text: str) -> np.ndarray:
        key = self._hash(f"{self.model_id}:{self._provider.dimension}:{text}")
        cached = self._embedding_cache.get(key)
        if cached is not None:
            return cached
        embedding = self._provider.embed(text)
        self._embedding_cache.set(key, embedding)
        return embedding

    def _ensure_difficulty_centroids(self) -> None:
        if self._easy_centroid is not None and self._hard_centroid is not None:
            return
        easy = self._provider.embed_batch(EASY_EXEMPLARS)
        hard = self._provider.embed_batch(HARD_EXEMPLARS)
        self._easy_centroid = _normalize(np.mean(np.stack(easy), axis=0))
        self._hard_centroid = _normalize(np.mean(np.stack(hard), axis=0))

    def _intrinsic_difficulty(self, embedding: np.ndarray) -> float:
        if self._easy_centroid is None or self._hard_centroid is None:
            return 0.0
        sim_hard = _cosine_similarity(embedding, self._hard_centroid)
        sim_easy = _cosine_similarity(embedding, self._easy_centroid)
        return float(1.0 / (1.0 + np.exp(-DIFFICULTY_SCALE * (sim_hard - sim_easy))))

    def is_ready(self) -> bool:
        return True

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()
