"""The prompt router of the tryaii SDK, as a data-only package.

H4 (router id ``h4-he-en``): a Model2Vec static embedding model (Unigram
tokenizer, 40k tokens, 256-d int8 vectors) and a one-hidden-layer
classification head over the 33 catalog benchmarks, trained for Hebrew and
English prompts. Exported by scripts/export-h4-model.py in the tryaii
repository. tryaii depends on this package and checks the payload hash
before it uses the files; nothing here is imported at run time except
:func:`model_dir`.

Payload hash: ``SHA256`` is the sha256 of ``SHA256SUMS`` -- one line
``<sha256>  <file>`` per payload file, sorted by file name -- so it covers
every file. ``SIZE_BYTES`` is the sum of the payload file sizes.
"""

from pathlib import Path

__version__ = "1.0.0"

#: The router id the scores carry.
MODEL_ID = "h4-he-en"
#: sha256 of SHA256SUMS (also pinned in tryaii.embeddings.model_store).
SHA256 = "b85b07c215479dce26cd283e3220e47924b08944f53b450e477e1d13a8cb9675"
#: Total size of the payload files.
SIZE_BYTES = 12_892_408
#: The payload files, in SHA256SUMS order.
FILES = ("classes.json", "head.safetensors", "meta.json", "tokenizer.json",
         "vectors.safetensors")


def model_dir() -> Path:
    """The directory that holds the payload files."""
    return Path(__file__).parent


def model_path() -> Path:
    """Alias of :func:`model_dir` (same shape as ``tryaii_minilm_int8``)."""
    return model_dir()
