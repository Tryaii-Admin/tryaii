"""
Centroid loader -- handles lazy initialization and model compatibility.

Loading priority:
    1. In-memory cache (already loaded)
    2. User's ~/.tryaii/centroids/ (previously generated for their model and
       this catalog kind+version), when its benchmark set matches the bundle's
    3. The catalog bundle's centroids.json (when built for this embedding
       model -- zero delay)
    4. Generate from the bundle's training queries (non-default embedding model)
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from tryaii.catalog.bundle import CatalogBundle, resolve_bundle
from tryaii.centroids.generator import CentroidGenerator, benchmark_fingerprint
from tryaii.config import TryaiiDreConfig
from tryaii.embeddings.base import BaseEmbeddingProvider

logger = logging.getLogger("tryaii.centroids")


class CentroidLoader:
    """
    Manages centroid lifecycle: load, validate, regenerate.

    For the catalog bundle's embedding model (all-MiniLM-L6-v2), centroids
    ship in the bundle -- zero first-run delay. For other models, centroids
    are generated from the bundle's training queries on first use and cached
    to disk. ``bundle`` (a CatalogBundle or bundle directory) defaults to the
    default catalog -- see :func:`tryaii.catalog.resolve_bundle`.
    """

    def __init__(
        self,
        config: TryaiiDreConfig,
        embedding_provider: BaseEmbeddingProvider,
        bundle=None,
    ):
        self._config = config
        self._provider = embedding_provider
        self._bundle: CatalogBundle = resolve_bundle(bundle)
        self._centroids: Optional[dict[str, np.ndarray]] = None
        self._generator = CentroidGenerator(embedding_provider, bundle=self._bundle)
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
            user_path = self.cache_path
            loaded = self._try_load(user_path)
            if loaded is not None:
                self._centroids = loaded
                return self._centroids

            # 2. Try the catalog bundle's centroids (built for its embedding model)
            loaded = self._try_bundle()
            if loaded is not None:
                self._centroids = loaded
                logger.info(
                    f"Loaded {self._bundle.kind} catalog centroids for "
                    f"{self._provider.model_name} ({len(loaded)} benchmarks)"
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
            return self._validated(centroids, metadata, str(path))
        except Exception as e:
            logger.warning(f"Failed to load centroids from {path}: {e}")
            return None

    def _try_bundle(self) -> Optional[dict[str, np.ndarray]]:
        """The catalog bundle's centroids, when they fit the current provider."""
        data = self._bundle.centroids
        centroids = {
            name: np.array(vector, dtype=np.float32)
            for name, vector in data["centroids"].items()
        }
        return self._validated(
            centroids, data.get("metadata", {}), f"the {self._bundle.kind} catalog bundle"
        )

    def _validated(
        self, centroids: dict[str, np.ndarray], metadata: dict, source: str
    ) -> Optional[dict[str, np.ndarray]]:
        """``centroids`` when they fit the provider and the bundle, else None."""
        saved_model = metadata.get("model", "")
        saved_dim = metadata.get("dimension", 0)

        # Validate model + dimension.
        if (saved_model != self._provider.model_name
                or saved_dim != self._provider.dimension):
            logger.debug(
                f"Centroid mismatch at {source} "
                f"(saved: {saved_model}/{saved_dim}, "
                f"current: {self._provider.model_name}/{self._provider.dimension})"
            )
            return None

        # Validate the benchmark set. We fingerprint the benchmarks actually
        # present (not a stored metadata value -- older files predate
        # fingerprinting and have none) and compare against the catalog
        # bundle's set, so a user cache generated for another catalog (or
        # another benchmark set) is never used.
        actual_fingerprint = benchmark_fingerprint(centroids.keys())
        expected_fingerprint = self._expected_fingerprint()
        if actual_fingerprint != expected_fingerprint:
            logger.debug(
                f"Centroid benchmark-set mismatch at {source} "
                f"(file fingerprint: {actual_fingerprint!r}, "
                f"expected: {expected_fingerprint!r})"
            )
            return None

        logger.debug(f"Loaded {len(centroids)} centroids from {source}")
        return centroids

    def _expected_fingerprint(self) -> str:
        """Fingerprint of the catalog bundle's benchmark set."""
        return CentroidGenerator.default_benchmark_fingerprint(self._bundle)

    @property
    def cache_path(self) -> Path:
        """This loader's user centroid cache: keyed by embedding model and by
        the catalog's kind + version (see ``TryaiiDreConfig.centroid_file_for``)."""
        return self._config.centroid_file_for(self._bundle)

    def _save(self, centroids: dict[str, np.ndarray]) -> None:
        """Write the user cache and drop this model's caches for older
        versions of the same catalog kind (a full catalog update makes them
        unreachable)."""
        path = self.cache_path
        self._generator.save(centroids, path)
        safe_name = self._config.embedding_model.replace("/", "__")
        prefix = f"centroids_{safe_name}__{self._bundle.kind}-"
        try:
            for stale in path.parent.glob(f"{prefix}*.json"):
                if stale.name != path.name:
                    stale.unlink()
        except OSError:
            pass

    @property
    def bundle(self) -> CatalogBundle:
        """The catalog bundle this loader serves."""
        return self._bundle

    def _regenerate(self) -> dict[str, np.ndarray]:
        """Generate centroids from training queries and save to user cache."""
        logger.info(
            f"Generating centroids for {self._provider.model_name} "
            f"(this only happens once per embedding model)..."
        )

        centroids = self._generator.generate(show_progress=True)

        # Save to user cache for future runs
        self._save(centroids)
        self._centroids = centroids

        logger.info(f"Centroids saved to {self.cache_path}")
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
        self._save(centroids)
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
        self._save(centroids)
        logger.info(f"Added custom benchmark '{benchmark_name}' with {len(queries)} queries")

        return new_centroid

    def remove_benchmark(self, benchmark_name: str) -> bool:
        """Remove a benchmark centroid."""
        centroids = self.get_centroids()
        if benchmark_name in centroids:
            del centroids[benchmark_name]
            self._config.ensure_dirs()
            self._save(centroids)
            return True
        return False

    @property
    def available_benchmarks(self) -> list[str]:
        """List all available benchmark names."""
        return list(self.get_centroids().keys())
