"""
Global configuration for TryAii-DRE.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger("tryaii")

# Load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# Default data directory: ~/.tryaii/
DEFAULT_DATA_DIR = Path.home() / ".tryaii"

# Default embedding model -- small, fast, runs on any modern CPU
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
DEFAULT_EMBEDDING_DIMENSION = 384


@dataclass
class CacheConfig:
    """Cache configuration."""

    embedding_cache_size: int = 300
    classification_cache_size: int = 150
    ttl_seconds: float = 300.0  # 5 minutes
    # Reserved for future distributed/Redis caching. NOT YET IMPLEMENTED:
    # setting this currently has no effect and the in-memory LRU cache is used.
    redis_url: Optional[str] = None

    def __post_init__(self):
        if self.redis_url:
            logger.warning(
                "redis_url is set but distributed/Redis caching is not yet "
                "implemented; falling back to the in-memory LRU cache."
            )


@dataclass
class TryaiiDreConfig:
    """
    Main configuration object.

    Can be passed to Router() to override defaults.
    Reads from environment variables if not set explicitly.
    """

    # Embedding model (sentence-transformers model name or path)
    embedding_model: str = field(
        default_factory=lambda: os.environ.get(
            "TRYAII_DRE_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
        )
    )

    # Where to store centroids, cached models, etc.
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("TRYAII_DRE_DATA_DIR", str(DEFAULT_DATA_DIR))
        )
    )

    # Cache settings
    cache: CacheConfig = field(default_factory=CacheConfig)

    # Scoring strategy
    strategy: Literal["balanced", "performance", "cost", "speed"] = "balanced"

    # OpenAI API key (only needed if using OpenAI embeddings instead of local)
    openai_api_key: Optional[str] = field(
        default_factory=lambda: os.environ.get("OPENAI_API_KEY")
    )

    # OpenRouter API key (only needed for active routing integration)
    openrouter_api_key: Optional[str] = field(
        default_factory=lambda: os.environ.get("OPENROUTER_API_KEY")
    )

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)

    @property
    def centroids_dir(self) -> Path:
        return self.data_dir / "centroids"

    @property
    def centroid_file(self) -> Path:
        """Path to centroids file for the current embedding model."""
        safe_name = self.embedding_model.replace("/", "__")
        return self.centroids_dir / f"centroids_{safe_name}.json"

    def ensure_dirs(self):
        """Create data directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.centroids_dir.mkdir(parents=True, exist_ok=True)
