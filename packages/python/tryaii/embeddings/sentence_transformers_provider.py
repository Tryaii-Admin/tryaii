"""
Embedding provider on sentence-transformers (PyTorch).

Not the default since TS-252 (the default is the ONNX build, see
:mod:`tryaii.embeddings.onnx_provider`). Used for any model name that is not
an ONNX model id -- custom sentence-transformers models -- and by the
maintainers to build the fp32 centroids. Needs the optional extra:
``pip install "tryaii[centroids]"``.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from tryaii.embeddings.base import BaseEmbeddingProvider


class SentenceTransformerEmbeddingProvider(BaseEmbeddingProvider):
    """
    Local embedding provider using sentence-transformers.

    Lazy-loads the model on first use to keep import times fast.

    Args:
        model_name: HuggingFace model name. Default: "all-MiniLM-L6-v2"
        device: Device to run on ("cpu", "cuda", "mps"). Default: auto-detect.
    """

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        device: Optional[str] = None,
    ):
        self._model_name = model_name
        self._device = device
        self._model = None
        self._dimension: Optional[int] = None

    def _ensure_loaded(self):
        """Lazy-load the model on first use."""
        if self._model is not None:
            return

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError(
                f"sentence-transformers is required for the embedding model "
                f"{self._model_name!r} (the default model runs on onnxruntime and "
                f"does not need it). Install it with: "
                f'pip install "tryaii[centroids]"'
            )

        self._model = SentenceTransformer(self._model_name, device=self._device)
        # Get dimension from a test embedding
        test = self._model.encode(["test"], convert_to_numpy=True)
        self._dimension = test.shape[1]

    def embed(self, text: str) -> np.ndarray:
        """Generate embedding for a single text."""
        self._ensure_loaded()
        embedding = self._model.encode(  # type: ignore[union-attr]
            [text], convert_to_numpy=True, normalize_embeddings=True
        )
        return embedding[0]

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        """Generate embeddings for multiple texts efficiently."""
        self._ensure_loaded()
        embeddings = self._model.encode(  # type: ignore[union-attr]
            texts, convert_to_numpy=True, normalize_embeddings=True, batch_size=32
        )
        return [embeddings[i] for i in range(len(texts))]

    @property
    def dimension(self) -> int:
        """Dimensionality of the embedding vectors."""
        self._ensure_loaded()
        assert self._dimension is not None
        return self._dimension

    @property
    def model_name(self) -> str:
        return self._model_name
