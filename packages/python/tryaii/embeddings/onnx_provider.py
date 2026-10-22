"""
ONNX embedding provider: all-MiniLM-L6-v2 as an int8 ONNX file on onnxruntime.

The default local embedding engine since TS-252. Same model and the same
pipeline as sentence-transformers' ``all-MiniLM-L6-v2`` (WordPiece tokens,
max 256, mean pooling over the attention mask, L2 normalisation), with int8
weights, so there is no PyTorch at run time. The vectors live in their own
space (``all-MiniLM-L6-v2-onnx-int8``): they are close to the fp32 ones
(cosine 0.999 or better; the weights are int8, the arithmetic fp32, so every
CPU computes the same vectors) but are only ever scored against centroids
built with this same file (catalog contract, versioned centroids).

The tokenizer (``tokenizer.json``, about 0.5 MB) ships in the package; the
model file (23 MB) ships in the ``tryaii-minilm-int8`` package, which
tryaii depends on; :mod:`tryaii.embeddings.model_store` resolves it
(``TRYAII_EMBEDDING_MODEL_PATH`` first) and checks its pinned sha256.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

import numpy as np

from tryaii.embeddings.base import BaseEmbeddingProvider
from tryaii.embeddings.model_store import (
    DEFAULT_ONNX_MODEL,
    MODELS,
    ModelUnavailableError,
    resolve_model_path,
    tokenizer_path,
)


class OnnxEmbeddingProvider(BaseEmbeddingProvider):
    """Local embeddings from an ONNX model file through onnxruntime.

    Lazy: the tokenizer and the session load on first use (and the model
    file is resolved and verified then), so constructing a provider is free.

    Args:
        model_name: A model id from :data:`tryaii.embeddings.model_store.MODELS`.
        model_path: An explicit model file (skips the resolution and the
            checksum; for tests and the export script).
    """

    def __init__(
        self,
        model_name: str = DEFAULT_ONNX_MODEL,
        *,
        model_path: Optional[str | Path] = None,
    ):
        if model_name not in MODELS:
            raise ValueError(f"unknown ONNX embedding model {model_name!r}")
        self._model_name = model_name
        self._spec = MODELS[model_name]
        self._model_path = Path(model_path) if model_path is not None else None
        self._session = None
        self._tokenizer = None
        self._input_names: tuple[str, ...] = ()
        self._lock = threading.Lock()

    # -- loading -----------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        with self._lock:
            if self._session is not None:
                return
            try:
                import onnxruntime as ort
                from tokenizers import Tokenizer
            except ImportError as exc:  # pragma: no cover - base dependencies
                raise ImportError(
                    "onnxruntime and tokenizers are required for local embeddings "
                    "(base dependencies of tryaii; reinstall with: pip install tryaii)"
                ) from exc

            path = self._model_path
            if path is None:
                path = resolve_model_path(self._model_name)
                if path is None:
                    raise ModelUnavailableError(self._model_name)

            tokenizer = Tokenizer.from_file(str(tokenizer_path(self._model_name)))
            tokenizer.enable_truncation(max_length=self._spec.max_length)
            tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

            options = ort.SessionOptions()
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            options.log_severity_level = 3
            session = ort.InferenceSession(str(path), sess_options=options,
                                           providers=["CPUExecutionProvider"])
            self._input_names = tuple(i.name for i in session.get_inputs())
            self._tokenizer = tokenizer
            self._session = session

    # -- embedding ---------------------------------------------------------

    def _encode(self, texts: list[str]) -> np.ndarray:
        self._ensure_loaded()
        encodings = self._tokenizer.encode_batch(texts)  # type: ignore[union-attr]
        ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = self._session.run(None, feeds)[0]  # type: ignore[union-attr]
        # Mean pooling over real tokens, then L2 (sentence-transformers'
        # Pooling(mean) + Normalize modules for this model).
        weights = mask[..., None].astype(np.float32)
        summed = (hidden * weights).sum(axis=1)
        counts = np.clip(weights.sum(axis=1), 1e-9, None)
        pooled = summed / counts
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / np.clip(norms, 1e-12, None)).astype(np.float32)

    def embed(self, text: str) -> np.ndarray:
        return self._encode([text])[0]

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        # One text per run, never padded batches: a text then goes through
        # exactly the same computation here, in embed() (one prompt at a time
        # in the monitor) and in the centroid build, independent of whatever
        # else is in the batch.
        return [self._encode([text])[0] for text in texts]

    @property
    def dimension(self) -> int:
        return self._spec.dimension

    @property
    def model_name(self) -> str:
        return self._model_name
