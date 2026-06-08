"""
Centroid loader -- handles lazy initialization and model compatibility.

Loading priority:
    1. In-memory cache (already loaded)
    2. User's ~/.tryaii/centroids/ (previously generated for their model)
    3. Bundled static file (ships with package for default model -- zero delay)
    4. Generate from training queries (only if using a non-default model)
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from tryaii.centroids.generator import CentroidGenerator, benchmark_fingerprint
from tryaii.config import TryaiiDreConfig
from tryaii.embeddings.base import BaseEmbeddingProvider

logger = logging.getLogger("tryaii.centroids")

# Path to bundled centroids (ships with the package)
BUNDLED_CENTROIDS_DIR = Path(__file__).parent / "data"


def _bundled_centroid_path(model_name: str) -> Path:
    """Path to the bundled centroid file for a given model."""
    safe_name = model_name.replace("/", "__")
    return BUNDLED_CENTROIDS_DIR / f"centroids_{safe_name}.json"


class CentroidLoader:
    """
    Manages centroid lifecycle: load, validate, regenerate.

    For the default embedding model (all-MiniLM-L6-v2), centroids are
    bundled with the package -- zero first-run delay. For other models,
    centroids are generated on first use and cached to disk.
    """

    def __init__(
        self,
        config: TryaiiDreConfig,
        embedding_provider: BaseEmbeddingProvider,
    ):
        self._config = config
        self._provider = embedding_provider
        self._centroids: Optional[dict[str, np.ndarray]] = None
        self._generator = CentroidGenerator(embedding_provider)
        # Guards lazy load/regenerate so concurrent asyncio.to_thread workers
        # don't each load the model / generate centroids.
        self._lock = threading.Lock()

    def get_centroids(self) -> dict[str, np.ndarray]:
        """
        Get centroids, loading from best available source.

        Priority: memory > user cache > bundled static > generate fresh.
        """
        # Double-checked locking: fast path without the lock, then re-check
        # under the lock so only one thread performs the load/regenerate.
        if self._centroids is not None:
            return self._centroids

        with self._lock:
            if self._centroids is not None:
                return self._centroids

            # 1. Try user's cached centroids (~/.tryaii/centroids/)
            self._config.ensure_dirs()
            user_path = self._config.centroid_file
            loaded = self._try_load(user_path)
            if loaded is not None:
                self._centroids = loaded
                return self._centroids

            # 2. Try bundled static centroids (ships with package)
            bundled_path = _bundled_centroid_path(self._provider.model_name)
            loaded = self._try_load(bundled_path)
            if loaded is not None:
                self._centroids = loaded
                logger.info(
                    f"Loaded bundled centroids for {self._provider.model_name} "
                    f"({len(loaded)} benchmarks)"
                )
                return self._centroids

            # 3. Generate fresh centroids (non-default model, first use)
            return self._regenerate()

    def _try_load(self, path: Path) -> Optional[dict[str, np.ndarray]]:
        """Try to load centroids from a file, validating compatibility."""
        if not path.exists():
            return None

        try:
            centroids, metadata = CentroidGenerator.load(path)

            saved_model = metadata.get("model", "")
            saved_dim = metadata.get("dimension", 0)

            # Validate model + dimension.
            if (saved_model != self._provider.model_name
                    or saved_dim != self._provider.dimension):
                logger.debug(
                    f"Centroid mismatch at {path} "
                    f"(saved: {saved_model}/{saved_dim}, "
                    f"current: {self._provider.model_name}/{self._provider.dimension})"
                )
                return None

            # Validate the benchmark set. We fingerprint the benchmarks actually
            # present in the file (not a stored metadata value -- older/bundled
            # files predate fingerprinting and have none) and compare against the
            # expected default set. This regenerates a file built against a
            # different benchmark set (the real risk) while still loading
            # pre-fingerprint bundled files whose benchmark set is unchanged.
            actual_fingerprint = benchmark_fingerprint(centroids.keys())
            expected_fingerprint = self._expected_fingerprint()
            if actual_fingerprint != expected_fingerprint:
                logger.debug(
                    f"Centroid benchmark-set mismatch at {path} "
                    f"(file fingerprint: {actual_fingerprint!r}, "
                    f"expected: {expected_fingerprint!r})"
                )
                return None

            logger.debug(f"Loaded {len(centroids)} centroids from {path}")
            return centroids
        except Exception as e:
            logger.warning(f"Failed to load centroids from {path}: {e}")
            return None

    def _expected_fingerprint(self) -> str:
        """Fingerprint of the benchmark set this loader expects on disk."""
        return CentroidGenerator.default_benchmark_fingerprint()

    def _regenerate(self) -> dict[str, np.ndarray]:
        """Generate centroids from training queries and save to user cache."""
        logger.info(
            f"Generating centroids for {self._provider.model_name} "
            f"(this only happens once per embedding model)..."
        )

        centroids = self._generator.generate(show_progress=True)

        # Save to user cache for future runs
        self._generator.save(centroids, self._config.centroid_file)
        self._centroids = centroids

        logger.info(f"Centroids saved to {self._config.centroid_file}")
        return centroids

    def regenerate(
        self,
        custom_queries: Optional[dict[str, list[str]]] = None,
    ) -> dict[str, np.ndarray]:
        """
        Force regeneration of centroids.

        Args:
            custom_queries: Optional custom training queries. If None, uses defaults.
        """
        centroids = self._generator.generate(
            training_queries=custom_queries, show_progress=True
        )
        self._generator.save(centroids, self._config.centroid_file)
        self._centroids = centroids
        return centroids

    def add_benchmark_centroid(
        self,
        benchmark_name: str,
        queries: list[str],
    ) -> np.ndarray:
        """
        Add a custom benchmark centroid to the existing set.

        Args:
            benchmark_name: Name of the new benchmark.
            queries: Representative queries for this benchmark.

        Returns:
            The generated centroid vector.
        """
        centroids = self.get_centroids()
        new_centroid = self._generator.generate_from_custom(benchmark_name, queries)
        centroids[benchmark_name] = new_centroid

        # Save updated centroids to user cache
        self._config.ensure_dirs()
        self._generator.save(centroids, self._config.centroid_file)
        logger.info(f"Added custom benchmark '{benchmark_name}' with {len(queries)} queries")

        return new_centroid

    def remove_benchmark(self, benchmark_name: str) -> bool:
        """Remove a benchmark centroid."""
        centroids = self.get_centroids()
        if benchmark_name in centroids:
            del centroids[benchmark_name]
            self._config.ensure_dirs()
            self._generator.save(centroids, self._config.centroid_file)
            return True
        return False

    @property
    def available_benchmarks(self) -> list[str]:
        """List all available benchmark names."""
        return list(self.get_centroids().keys())
