"""
Local embedding provider: the on-device default.

Default model: ``all-MiniLM-L6-v2-onnx-int8`` -- all-MiniLM-L6-v2 (22M
params, 384 dimensions) as an int8 ONNX file on onnxruntime (TS-252):
no PyTorch, about 23 MB of weights downloaded once (or pre-placed with
``TRYAII_EMBEDDING_MODEL_PATH``), no API key.

Any other model name (``all-MiniLM-L6-v2`` itself, or any sentence-transformers
model) runs on sentence-transformers, which is an optional extra
(``pip install "tryaii[centroids]"``).
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from tryaii.embeddings.base import BaseEmbeddingProvider
from tryaii.embeddings.model_store import DEFAULT_ONNX_MODEL, is_onnx_model


class LocalEmbeddingProvider(BaseEmbeddingProvider):
    """
    Local embeddings, backend chosen by model name.

    An ONNX model id (see :data:`tryaii.embeddings.model_store.MODELS`) runs on
    :class:`~tryaii.embeddings.onnx_provider.OnnxEmbeddingProvider`; any other
    name on :class:`~tryaii.embeddings.sentence_transformers_provider.
    SentenceTransformerEmbeddingProvider`.
    Lazy: nothing loads until the first embedding.

    Args:
        model_name: Default ``all-MiniLM-L6-v2-onnx-int8``.
        device: sentence-transformers only ("cpu", "cuda", "mps"; default auto).
    """

    def __init__(self, model_name: str = DEFAULT_ONNX_MODEL, device: Optional[str] = None):
        if is_onnx_model(model_name):
            from tryaii.embeddings.onnx_provider import OnnxEmbeddingProvider

            self._backend: BaseEmbeddingProvider = OnnxEmbeddingProvider(model_name)
        else:
            from tryaii.embeddings.sentence_transformers_provider import (
                SentenceTransformerEmbeddingProvider,
            )

            self._backend = SentenceTransformerEmbeddingProvider(model_name, device=device)

    @property
    def backend(self) -> BaseEmbeddingProvider:
        return self._backend

    def embed(self, text: str) -> np.ndarray:
        return self._backend.embed(text)

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return self._backend.embed_batch(texts)

    @property
    def dimension(self) -> int:
        return self._backend.dimension

    @property
    def model_name(self) -> str:
        return self._backend.model_name
