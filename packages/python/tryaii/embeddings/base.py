"""
Abstract embedding provider interface.

Allows swapping between local (sentence-transformers) and cloud (OpenAI)
embedding backends without changing the classifier logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class BaseEmbeddingProvider(ABC):
    """Base class for embedding providers."""

    @abstractmethod
    def embed(self, text: str) -> np.ndarray:
        """
        Generate an embedding vector for a single text.

        Args:
            text: Input text to embed.

        Returns:
            1-D numpy array of floats (the embedding vector).
        """
        ...

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        """
        Generate embeddings for multiple texts efficiently.

        Args:
            texts: List of input texts.

        Returns:
            List of 1-D numpy arrays.
        """
        ...

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Dimensionality of the embedding vectors."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Name/identifier of the embedding model."""
        ...
