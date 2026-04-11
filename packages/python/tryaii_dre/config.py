"""
Global configuration for TryAii-DRE.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

# Load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# Default data directory: ~/.tryaii_dre/
DEFAULT_DATA_DIR = Path.home() / ".tryaii_dre"

# Default embedding model -- small, fast, runs on any modern CPU
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
DEFAULT_EMBEDDING_DIMENSION = 384


@dataclass
class CacheConfig:
    """Cache configuration."""

    embedding_cache_size: int = 300
    classification_cache_size: int = 150
    ttl_seconds: float = 300.0  # 5 minutes
    # Optional Redis URL for distributed caching
    redis_url: Optional[str] = None


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
