"""
Centroid generator -- creates benchmark centroids from training queries.

Centroids are the average embedding of all training queries for a benchmark.
They are used by the EmbeddingClassifier to measure how similar a user's
prompt is to each benchmark category.

Centroids are regenerated when the embedding model changes, because different
models produce different vector spaces.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np

from tryaii_dre.embeddings.base import BaseEmbeddingProvider

logger = logging.getLogger("tryaii_dre.centroids")

# Bundled training queries
TRAINING_QUERIES_PATH = Path(__file__).parent / "data" / "training_queries.json"


def benchmark_fingerprint(benchmark_names) -> str:
    """
    Stable fingerprint of the benchmark set present in a centroid file.

    Defined as the sorted benchmark names joined by "|". Used to detect when
    a cached/bundled centroid file was generated against a different benchmark
    set and must be regenerated. Must stay identical in the Node SDK.
    """
    return "|".join(sorted(benchmark_names))


class CentroidGenerator:
    """
    Generates and manages benchmark centroids.

    Centroids are the average embedding vector of representative queries
    for each benchmark. When a user sends a prompt, we compute cosine
    similarity between their prompt's embedding and each centroid to
    determine what kind of task they're asking about.

    Usage:
        generator = CentroidGenerator(embedding_provider)
        centroids = generator.generate()           # Generate from default queries
        generator.save(centroids, "path/to.json")  # Save to disk
        centroids = generator.load("path/to.json") # Load from disk
    """

    def __init__(self, embedding_provider: BaseEmbeddingProvider):
        self._provider = embedding_provider

    def generate(
        self,
        training_queries: Optional[dict[str, list[str]]] = None,
        show_progress: bool = True,
    ) -> dict[str, np.ndarray]:
        """
        Generate centroids from training queries.

        Args:
            training_queries: Dict of benchmark_name -> list of queries.
                              If None, uses bundled default queries.
            show_progress: Print progress during generation.

        Returns:
            Dict of benchmark_name -> centroid vector (numpy array).
        """
        if training_queries is None:
            training_queries = self._load_default_queries()

        centroids: dict[str, np.ndarray] = {}

        for benchmark, queries in training_queries.items():
            if show_progress:
                logger.info(f"Generating centroid for {benchmark} ({len(queries)} queries)...")

            # Reject empty query lists -- they would produce an all-NaN centroid
            if not queries:
                raise ValueError(
                    f"Cannot generate centroid for '{benchmark}': no training queries provided"
                )

            # Embed all queries for this benchmark
            embeddings = self._provider.embed_batch(queries)

            # Centroid = average of all embeddings, then normalize
            centroid = np.mean(embeddings, axis=0)
            norm = np.linalg.norm(centroid)
            if norm > 0:
                centroid = centroid / norm

            # Refuse to store a non-finite centroid (NaN/inf) -- skip the benchmark
            if not np.all(np.isfinite(centroid)):
                logger.warning(
                    f"Skipping benchmark '{benchmark}': computed centroid is non-finite"
                )
                continue

            centroids[benchmark] = centroid

        if show_progress:
            logger.info(f"Generated {len(centroids)} centroids "
                       f"(dim={self._provider.dimension})")

        return centroids

    def generate_from_custom(
        self,
        benchmark_name: str,
        queries: list[str],
    ) -> np.ndarray:
        """
        Generate a single centroid for a custom benchmark.

        Args:
            benchmark_name: Name of the benchmark.
            queries: Representative queries for this benchmark.

        Returns:
            Centroid vector (numpy array).
        """
        # Reject empty query lists -- they would produce an all-NaN centroid
        if not queries:
            raise ValueError(
                f"Cannot generate centroid for '{benchmark_name}': no queries provided"
            )

        embeddings = self._provider.embed_batch(queries)
        centroid = np.mean(embeddings, axis=0)
        norm = np.linalg.norm(centroid)
        if norm > 0:
            centroid = centroid / norm

        # Refuse to return a non-finite centroid (NaN/inf)
        if not np.all(np.isfinite(centroid)):
            raise ValueError(
                f"Computed centroid for '{benchmark_name}' is non-finite"
            )
        return centroid

    def save(self, centroids: dict[str, np.ndarray], path: str | Path) -> None:
        """Save centroids to a JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "metadata": {
                "model": self._provider.model_name,
                "dimension": self._provider.dimension,
                "benchmark_count": len(centroids),
                # Fingerprint of the benchmark set so the loader can reject a
                # file generated against a different set of benchmarks.
                "benchmark_fingerprint": benchmark_fingerprint(centroids.keys()),
            },
            "centroids": {
                name: vector.tolist() for name, vector in centroids.items()
            },
        }

        # Write to temp file then atomically rename
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, path)
        except BaseException:
            os.unlink(tmp_path)
            raise

        logger.info(f"Saved {len(centroids)} centroids to {path}")

    @staticmethod
    def load(path: str | Path) -> tuple[dict[str, np.ndarray], dict]:
        """
        Load centroids from a JSON file.

        Returns:
            Tuple of (centroids dict, metadata dict).
        """
        with open(path) as f:
            data = json.load(f)

        centroids = {
            name: np.array(vector, dtype=np.float32)
            for name, vector in data["centroids"].items()
        }

        return centroids, data.get("metadata", {})

    def _load_default_queries(self) -> dict[str, list[str]]:
        """Load bundled training queries."""
        with open(TRAINING_QUERIES_PATH) as f:
            data = json.load(f)

        return {
            name: bench_data["queries"]
            for name, bench_data in data["benchmarks"].items()
        }

    @staticmethod
    def default_benchmark_fingerprint() -> str:
        """Fingerprint of the bundled default benchmark set."""
        with open(TRAINING_QUERIES_PATH) as f:
            data = json.load(f)
        return benchmark_fingerprint(data["benchmarks"].keys())
