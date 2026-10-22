"""
Where the ONNX embedding model file comes from (TS-252).

The file ships in its own PyPI package, ``tryaii-minilm-int8``, which
``tryaii`` depends on (pinned exactly): ``pip install tryaii`` brings it in,
through the same index, mirror or cache as every other dependency. Nothing is
downloaded at run time.

Resolution order for a model id:

1. ``TRYAII_EMBEDDING_MODEL_PATH``: a pre-placed model file (or a directory
   holding ``model.onnx``), e.g. for a machine that cannot install the
   package. When set, it is the only source (a wrong file is an error, not a
   reason to fall back silently).
2. The installed model package (:attr:`ModelSpec.package`).

Every file must match the size and sha256 pinned in :data:`MODELS`: vectors
from any other file would be scored against centroids that were not built
from it. When there is no verified file, :func:`resolve_model_path` returns
None and :func:`last_error` says why; callers fail open (the monitor records
metadata only and sends the model in your code).

How a missing model is reported (:func:`report_unavailable`): the state is
kept in ``<data dir>/embedding-model.json`` (shown by ``tryaii monitor
status``), and the warning is logged at most once per machine per day for the
same reason -- every other time at debug level -- so a long-lived fleet does
not repeat it on every process start. The library never prints.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import importlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tryaii.embeddings")

ENV_MODEL_PATH = "TRYAII_EMBEDDING_MODEL_PATH"
MODEL_FILE = "model.onnx"
STATE_FILE = "embedding-model.json"

DATA_DIR = Path(__file__).parent / "data"


@dataclass(frozen=True)
class ModelSpec:
    """An ONNX embedding model and the package that ships it."""

    model_id: str
    #: sha256 of the exact model file (scripts/export-onnx-embedding.py).
    sha256: str
    size_bytes: int
    dimension: int
    max_length: int
    #: The fp32 model whose space this one approximates (for docs and tests).
    base_model: str
    #: Import name of the package that ships the file (exposes ``model_path()``).
    package: str
    #: Its distribution name, for messages (``pip install ...``).
    distribution: str


#: The ONNX models this SDK can run. Centroids for each are published in the
#: catalog bundle next to the base model's (catalog contract v1.3).
MODELS: dict[str, ModelSpec] = {
    "all-MiniLM-L6-v2-onnx-int8": ModelSpec(
        model_id="all-MiniLM-L6-v2-onnx-int8",
        sha256="8199be6f699a395f99f89df973f9dc5285fbcfcdb2ae1a4e1201dab014f3d2a6",
        size_bytes=22928619,
        dimension=384,
        max_length=256,
        base_model="all-MiniLM-L6-v2",
        package="tryaii_minilm_int8",
        distribution="tryaii-minilm-int8",
    ),
}

DEFAULT_ONNX_MODEL = "all-MiniLM-L6-v2-onnx-int8"


class ModelUnavailableError(RuntimeError):
    """No verified model file is installed or placed."""

    def __init__(self, model_id: str, reason: Optional[str] = None):
        reason = reason or last_error() or "not found"
        spec = MODELS.get(model_id)
        fix = (f"reinstall it (pip install --force-reinstall {spec.distribution}) or "
               if spec else "")
        super().__init__(
            f"embedding model {model_id} is not available ({reason}); {fix}"
            f"set {ENV_MODEL_PATH} to a copy of the model file"
        )
        self.model_id = model_id
        self.reason = reason


def is_onnx_model(model_id: str) -> bool:
    return model_id in MODELS


def tokenizer_path(model_id: str) -> Path:
    return DATA_DIR / model_id / "tokenizer.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# -- verification memo: (path, size, mtime_ns) -> ok -------------------------
_VERIFIED: dict[tuple, bool] = {}
_LOCK = threading.Lock()
_LAST_ERROR: dict[str, Optional[str]] = {"error": None}


def last_error() -> Optional[str]:
    """Why the last :func:`resolve_model_path` returned None (or None)."""
    return _LAST_ERROR["error"]


def _fail(reason: str) -> None:
    _LAST_ERROR["error"] = reason


def _verified(path: Path, spec: ModelSpec) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    key = (str(path), st.st_size, st.st_mtime_ns)
    with _LOCK:
        hit = _VERIFIED.get(key)
    if hit is None:
        hit = st.st_size == spec.size_bytes and sha256_file(path) == spec.sha256
        with _LOCK:
            _VERIFIED[key] = hit
    return hit


def _override_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    return path / MODEL_FILE if path.is_dir() else path


def package_model_path(spec: ModelSpec) -> Optional[Path]:
    """The file of the installed model package, or None when not installed."""
    try:
        module = importlib.import_module(spec.package)
    except ImportError:
        return None
    return Path(module.model_path())


def resolve_model_path(model_id: str) -> Optional[Path]:
    """A verified model file for ``model_id``, or None (see the module
    docstring for the order). Never raises for a missing or wrong file;
    :func:`last_error` says why."""
    spec = MODELS.get(model_id)
    if spec is None:
        _fail(f"unknown model {model_id}")
        return None

    override = os.environ.get(ENV_MODEL_PATH)
    if override:
        path = _override_path(override)
        if _verified(path, spec):
            _LAST_ERROR["error"] = None
            return path
        _fail(f"{ENV_MODEL_PATH} points at a missing file or one that does not match")
        return None

    path = package_model_path(spec)
    if path is None:
        _fail(f"package {spec.distribution} is not installed")
        return None
    if not _verified(path, spec):
        _fail(f"the installed {spec.distribution} does not match the expected sha256")
        return None
    _LAST_ERROR["error"] = None
    return path


# -- reporting a missing model (throttled) ------------------------------------
_WARNED_THIS_PROCESS: set = set()


def _data_dir(data_dir: Optional[Path]) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    env = os.environ.get("TRYAII_DRE_DATA_DIR")
    return Path(env) if env else Path.home() / ".tryaii"


def state_path(data_dir: Optional[Path] = None) -> Path:
    return _data_dir(data_dir) / STATE_FILE


def read_state(data_dir: Optional[Path] = None) -> Optional[dict]:
    """``embedding-model.json`` when the model was last found unavailable,
    else None: ``{"model", "reason", "since", "last_warned"}``."""
    try:
        doc = json.loads(state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("model") else None


def _write_state(data_dir: Optional[Path], doc: dict) -> None:
    from tryaii.auth.store import write_atomic

    write_atomic(state_path(data_dir), (json.dumps(doc) + "\n").encode("utf-8"),
                 prefix=".embedding-model-")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def report_unavailable(model_id: str, reason: str, *, data_dir: Optional[Path] = None,
                       log: Optional[logging.Logger] = None, message: str = "",
                       today: Optional[str] = None) -> bool:
    """Record that ``model_id`` is unavailable and log it -- as a warning at
    most once per machine per local day for the same reason (once per process
    when the data dir is not writable), at debug level otherwise. Returns True
    when the warning was logged. Never raises."""
    log = log or logger
    today = today or _dt.date.today().isoformat()
    text = message or f"embedding model {model_id} is not available ({reason})"
    previous = read_state(data_dir) or {}
    same = previous.get("model") == model_id and previous.get("reason") == reason
    doc = {
        "model": model_id,
        "reason": reason,
        "since": previous.get("since") if same and previous.get("since") else _now_iso(),
        "last_warned": previous.get("last_warned") if same else None,
    }
    warn = doc["last_warned"] != today
    key = (model_id, reason)
    if warn:
        doc["last_warned"] = today
        try:
            _write_state(data_dir, doc)
        except OSError:
            # Read-only data dir: still never more than once per process.
            warn = key not in _WARNED_THIS_PROCESS
    elif not same:
        try:
            _write_state(data_dir, doc)
        except OSError:
            pass
    if warn:
        _WARNED_THIS_PROCESS.add(key)
        log.warning(text)
    else:
        log.debug(text)
    return warn


def clear_unavailable(data_dir: Optional[Path] = None,
                      log: Optional[logging.Logger] = None) -> None:
    """The model is available (again): drop the state file, saying so once."""
    path = state_path(data_dir)
    if not path.exists():
        return
    try:
        path.unlink()
    except OSError:
        return
    (log or logger).info("embedding model available again; classification is back on")


def reset_cache() -> None:
    """Forget verified files and per-process warnings (tests)."""
    with _LOCK:
        _VERIFIED.clear()
    _WARNED_THIS_PROCESS.clear()
    _LAST_ERROR["error"] = None
