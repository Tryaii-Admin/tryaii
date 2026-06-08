"""
OpenAI embedding provider (optional).

Uses OpenAI's text-embedding-3-small by default.
Requires: pip install tryaii[openai]
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from tryaii.embeddings.base import BaseEmbeddingProvider


class OpenAIEmbeddingProvider(BaseEmbeddingProvider):
    """
    OpenAI API-based embedding provider.

    Args:
        api_key: OpenAI API key. Falls back to OPENAI_API_KEY env var.
        model: OpenAI embedding model name.
    """

    DIMENSIONS = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "text-embedding-3-small",
    ):
        self._model_name = model
        self._api_key = api_key
        self._client = None
        self._dimension = self.DIMENSIONS.get(model, 1536)

    def _ensure_client(self):
        if self._client is not None:
            return

        try:
            import openai
        except ImportError:
            raise ImportError(
                "openai package is required for OpenAI embeddings. "
                "Install with: pip install tryaii[openai]"
            )

        kwargs = {}
        if self._api_key:
            kwargs["api_key"] = self._api_key

        self._client = openai.OpenAI(timeout=30.0, max_retries=2, **kwargs)

    def embed(self, text: str) -> np.ndarray:
        self._ensure_client()
        response = self._client.embeddings.create(  # type: ignore[union-attr]
            input=[text], model=self._model_name
        )
        return np.array(response.data[0].embedding, dtype=np.float32)

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        self._ensure_client()
        response = self._client.embeddings.create(  # type: ignore[union-attr]
            input=texts, model=self._model_name
        )
        return [
            np.array(item.embedding, dtype=np.float32)
            for item in sorted(response.data, key=lambda x: x.index)
        ]

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return self._model_name
