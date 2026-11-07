"""
Static embedding provider: the H4 router's Model2Vec model on numpy.

The default router's encoder since H4 (router id ``h4-he-en``). A Model2Vec
static model is a table of token vectors: a text's vector is the mean of the
vectors of its tokens, L2-normalised. No neural network runs, so there is no
PyTorch and no onnxruntime: only numpy and ``tokenizers`` (both base
dependencies).

The payload (``vectors.safetensors``, ``tokenizer.json``, ``meta.json``, plus
the head files the classifier reads) ships in the ``tryaii-router-h4``
package; :func:`tryaii.embeddings.model_store.resolve_router_dir` finds it
(``TRYAII_ROUTER_MODEL_PATH`` first) and checks its pinned hash.

Inference, exactly as model2vec 0.10 ``StaticModel.encode`` (the POC that
trained H4) computes it:

1. cut the text to ``char_cap`` code points (``max_tokens *
   median_token_length`` = 700 * 5);
2. tokenize without special tokens, truncated to ``max_tokens`` (700) tokens,
   then drop every unknown-token id;
3. mean of the int8 rows of those ids -- summed as integers (exact, so the
   order of accumulation cannot change the result), divided once in float64,
   cast to float32;
4. L2-normalise (float32); a text with no known tokens is the zero vector.
"""

from __future__ import annotations

import json
import struct
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from tryaii.embeddings.base import BaseEmbeddingProvider
from tryaii.embeddings.model_store import (
    DEFAULT_ROUTER_MODEL,
    ROUTERS,
    RouterUnavailableError,
    resolve_router_dir,
)

_ST_DTYPES = {"I8": np.dtype("<i1"), "F32": np.dtype("<f4"), "I64": np.dtype("<i8")}


def load_safetensors(path: Path) -> dict[str, np.ndarray]:
    """The tensors of a safetensors file (I8 / F32 / I64 only). A tiny
    reader, so the SDK needs no ``safetensors`` package."""
    data = Path(path).read_bytes()
    if len(data) < 8:
        raise ValueError(f"{path}: not a safetensors file")
    (n,) = struct.unpack("<Q", data[:8])
    header = json.loads(data[8:8 + n].decode("utf-8"))
    body = memoryview(data)[8 + n:]
    out: dict[str, np.ndarray] = {}
    for name, info in header.items():
        if name == "__metadata__":
            continue
        dtype = _ST_DTYPES.get(info["dtype"])
        if dtype is None:
            raise ValueError(f"{path}: unsupported dtype {info['dtype']} for {name}")
        start, end = info["data_offsets"]
        shape = tuple(int(x) for x in info["shape"])
        arr = np.frombuffer(body[start:end], dtype=dtype)
        if arr.size != int(np.prod(shape, dtype=np.int64)):
            raise ValueError(f"{path}: {name} has the wrong size")
        out[name] = arr.reshape(shape).astype(dtype.newbyteorder("="), copy=True)
    return out


class StaticEmbeddingProvider(BaseEmbeddingProvider):
    """Model2Vec static embeddings (the H4 router's encoder).

    Lazy and thread-safe: the payload is resolved, verified and loaded on
    first use, under a lock.

    Args:
        model_name: A router id from :data:`tryaii.embeddings.model_store.ROUTERS`.
        model_dir: An explicit payload directory (skips the resolution and the
            hash check; for tests and the export check).
    """

    def __init__(self, model_name: str = DEFAULT_ROUTER_MODEL, *,
                 model_dir: Optional[str | Path] = None):
        if model_name not in ROUTERS:
            raise ValueError(f"unknown router model {model_name!r}")
        self._model_name = model_name
        self._spec = ROUTERS[model_name]
        self._explicit_dir = Path(model_dir) if model_dir is not None else None
        self._dir: Optional[Path] = None
        self._tokenizer = None
        self._vectors: Optional[np.ndarray] = None
        self._meta: dict = {}
        self._lock = threading.Lock()

    # -- loading -----------------------------------------------------------

    def ensure_loaded(self) -> Path:
        """Load the payload (once); return its directory. Raises
        RouterUnavailableError when there is no verified payload, and any
        load error for a broken one."""
        if self._vectors is not None:
            return self._dir  # type: ignore[return-value]
        with self._lock:
            if self._vectors is not None:
                return self._dir  # type: ignore[return-value]
            try:
                from tokenizers import Tokenizer
            except ImportError as exc:  # pragma: no cover - base dependency
                raise ImportError("tokenizers is required for the local router "
                                  "(a base dependency of tryaii; pip install tryaii)") from exc
            directory = self._explicit_dir
            if directory is None:
                directory = resolve_router_dir(self._model_name)
                if directory is None:
                    raise RouterUnavailableError(self._model_name)
            meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
            if meta.get("router_id") != self._model_name:
                raise ValueError(f"{directory}: meta.json is for {meta.get('router_id')!r}")
            vectors = load_safetensors(directory / "vectors.safetensors")["embeddings"]
            if vectors.dtype != np.int8 or vectors.ndim != 2 or vectors.shape[1] != meta["dim"]:
                raise ValueError(f"{directory}: unexpected vectors {vectors.dtype} {vectors.shape}")
            tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
            tokenizer.no_padding()
            tokenizer.enable_truncation(max_length=int(meta["max_tokens"]))
            if tokenizer.get_vocab_size(with_added_tokens=True) != vectors.shape[0]:
                raise ValueError(f"{directory}: tokenizer and vectors disagree on the vocabulary")
            self._meta = meta
            self._tokenizer = tokenizer
            self._dir = Path(directory)
            self._vectors = vectors
            return self._dir

    @property
    def meta(self) -> dict:
        self.ensure_loaded()
        return self._meta

    @property
    def directory(self) -> Path:
        return self.ensure_loaded()

    # -- embedding ---------------------------------------------------------

    def token_ids(self, text: str) -> list[int]:
        """The ids the vector is the mean of (after the char cap, the token
        cap and dropping the unknown token)."""
        self.ensure_loaded()
        meta = self._meta
        text = text[: int(meta["char_cap"])]
        enc = self._tokenizer.encode(text, add_special_tokens=False)  # type: ignore[union-attr]
        unk = meta.get("unk_id")
        return [i for i in enc.ids if i != unk]

    def _encode(self, text: str) -> np.ndarray:
        ids = self.token_ids(text)
        dim = int(self._meta["dim"])
        if not ids:
            return np.zeros(dim, dtype=np.float32)
        rows = self._vectors[np.asarray(ids, dtype=np.int64)]  # type: ignore[index]
        # Exact integer sum (|sum| <= 700 * 128), one division: the same float64
        # mean numpy takes of int8 rows, independent of summation order.
        total = rows.sum(axis=0, dtype=np.int64)
        mean = (total.astype(np.float64) / len(ids)).astype(np.float32)
        norm = np.float32(np.sqrt(np.dot(mean.astype(np.float64), mean.astype(np.float64))))
        if not np.isfinite(norm) or norm <= 0:
            return np.zeros(dim, dtype=np.float32)
        return (mean / norm).astype(np.float32)

    def embed(self, text: str) -> np.ndarray:
        return self._encode(text)

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [self._encode(t) for t in texts]

    @property
    def dimension(self) -> int:
        return self._spec.dimension

    @property
    def model_name(self) -> str:
        return self._model_name
