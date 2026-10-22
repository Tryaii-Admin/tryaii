"""The embedding model of the tryaii SDK, as a data-only package (TS-252).

all-MiniLM-L6-v2 (sentence-transformers, Apache-2.0) exported to ONNX with
int8 weights by scripts/export-onnx-embedding.py in the tryaii repository.
tryaii depends on this package and checks the file's sha256 before it runs
it; nothing here is imported at run time except :func:`model_path`.
"""

from pathlib import Path

__version__ = "1.0.0"

#: The embedding model id the vectors and centroids carry.
MODEL_ID = "all-MiniLM-L6-v2-onnx-int8"
#: sha256 and size of ``model.onnx`` (also pinned in tryaii.embeddings.model_store).
SHA256 = "8199be6f699a395f99f89df973f9dc5285fbcfcdb2ae1a4e1201dab014f3d2a6"
SIZE_BYTES = 22_928_619


def model_path() -> Path:
    """The installed ``model.onnx``."""
    return Path(__file__).parent / "model.onnx"
