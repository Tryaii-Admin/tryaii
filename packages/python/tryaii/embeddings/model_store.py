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

The router payload (H4, ``h4-he-en``)
-------------------------------------

The default router since H4 is a static embedding model plus a classification
head, shipped as a directory of five files in its own package,
``tryaii-router-h4`` (:data:`ROUTERS`). It resolves the same way:
``TRYAII_ROUTER_MODEL_PATH`` (a directory holding the payload) first, else the
installed package; every payload must match the pinned hash -- the sha256 of
its ``SHA256SUMS`` text (one ``<sha256>  <file>`` line per payload file,
sorted by name), recomputed here from the files themselves -- and total size.
When H4 is unavailable the router falls back to the MiniLM path above; that is
recorded in ``<data dir>/router-model.json`` (:data:`ROUTER_STATE_FILE`) with
the same once-a-day warning, so ``tryaii monitor status`` can say so.
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

ENV_ROUTER_MODEL_PATH = "TRYAII_ROUTER_MODEL_PATH"
ROUTER_STATE_FILE = "router-model.json"

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


@dataclass(frozen=True)
class RouterSpec:
    """A router payload (static embedding model + classification head) and
    the package that ships it (scripts/export-h4-model.py)."""

    router_id: str
    #: sha256 of the payload's SHA256SUMS text (see the module docstring).
    sha256: str
    #: Sum of the payload file sizes.
    size_bytes: int
    #: The payload files (SHA256SUMS order).
    files: tuple
    dimension: int
    #: Import name of the package that ships it (exposes ``model_dir()``).
    package: str
    #: Its distribution name, for messages (``pip install ...``).
    distribution: str


ROUTERS: dict[str, RouterSpec] = {
    "h4-he-en": RouterSpec(
        router_id="h4-he-en",
        sha256="b85b07c215479dce26cd283e3220e47924b08944f53b450e477e1d13a8cb9675",
        size_bytes=12892408,
        files=("classes.json", "head.safetensors", "meta.json", "tokenizer.json",
               "vectors.safetensors"),
        dimension=256,
        package="tryaii_router_h4",
        distribution="tryaii-router-h4",
    ),
}

DEFAULT_ROUTER_MODEL = "h4-he-en"


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


# -- router payloads ------------------------------------------------------------
_ROUTER_ERROR: dict[str, Optional[str]] = {"error": None}


def last_router_error() -> Optional[str]:
    """Why the last :func:`resolve_router_dir` returned None (or None)."""
    return _ROUTER_ERROR["error"]


def payload_manifest(directory: Path, files) -> tuple[str, int]:
    """``(SHA256SUMS text, total size)`` computed from the files in
    ``directory``. Raises OSError for a missing file."""
    lines, size = [], 0
    for name in sorted(files):
        path = Path(directory) / name
        size += path.stat().st_size
        lines.append(f"{sha256_file(path)}  {name}\n")
    return "".join(lines), size


def payload_sha256(directory: Path, files) -> tuple[str, int]:
    """``(payload hash, total size)`` of the files in ``directory``."""
    text, size = payload_manifest(directory, files)
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), size


def _router_verified(directory: Path, spec: RouterSpec) -> bool:
    try:
        stats = [(name, (Path(directory) / name).stat()) for name in spec.files]
    except OSError:
        return False
    key = ("router", str(directory)) + tuple(
        (name, st.st_size, st.st_mtime_ns) for name, st in stats)
    with _LOCK:
        hit = _VERIFIED.get(key)
    if hit is None:
        if sum(st.st_size for _, st in stats) != spec.size_bytes:
            hit = False
        else:
            try:
                digest, _size = payload_sha256(directory, spec.files)
            except OSError:
                digest = ""
            hit = digest == spec.sha256
        with _LOCK:
            _VERIFIED[key] = hit
    return hit


def package_router_dir(spec: RouterSpec) -> Optional[Path]:
    """The payload directory of the installed router package, or None."""
    try:
        module = importlib.import_module(spec.package)
    except ImportError:
        return None
    return Path(module.model_dir())


def resolve_router_dir(router_id: str = DEFAULT_ROUTER_MODEL) -> Optional[Path]:
    """A verified payload directory for ``router_id``, or None
    (``TRYAII_ROUTER_MODEL_PATH`` first, else the installed package). Never
    raises; :func:`last_router_error` says why."""
    spec = ROUTERS.get(router_id)
    if spec is None:
        _ROUTER_ERROR["error"] = f"unknown router {router_id}"
        return None
    override = os.environ.get(ENV_ROUTER_MODEL_PATH)
    if override:
        path = Path(override).expanduser()
        if _router_verified(path, spec):
            _ROUTER_ERROR["error"] = None
            return path
        _ROUTER_ERROR["error"] = (f"{ENV_ROUTER_MODEL_PATH} points at a missing directory "
                                  f"or one that does not match")
        return None
    try:
        path = package_router_dir(spec)
    except Exception:  # noqa: BLE001 - a broken package is "not installed"
        path = None
    if path is None:
        _ROUTER_ERROR["error"] = f"package {spec.distribution} is not installed"
        return None
    if not _router_verified(path, spec):
        _ROUTER_ERROR["error"] = (f"the installed {spec.distribution} does not match "
                                  f"the expected sha256")
        return None
    _ROUTER_ERROR["error"] = None
    return path


class RouterUnavailableError(RuntimeError):
    """No verified router payload is installed or placed."""

    def __init__(self, router_id: str, reason: Optional[str] = None):
        reason = reason or last_router_error() or "not found"
        spec = ROUTERS.get(router_id)
        fix = (f"reinstall it (pip install --force-reinstall {spec.distribution}) or "
               if spec else "")
        super().__init__(
            f"router model {router_id} is not available ({reason}); {fix}"
            f"set {ENV_ROUTER_MODEL_PATH} to a copy of its directory"
        )
        self.router_id = router_id
        self.reason = reason


# -- reporting a missing model (throttled) ------------------------------------
_WARNED_THIS_PROCESS: set = set()


def _data_dir(data_dir: Optional[Path]) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    env = os.environ.get("TRYAII_DRE_DATA_DIR")
    return Path(env) if env else Path.home() / ".tryaii"


def state_path(data_dir: Optional[Path] = None, state_file: str = STATE_FILE) -> Path:
    return _data_dir(data_dir) / state_file


def read_state(data_dir: Optional[Path] = None,
               state_file: str = STATE_FILE) -> Optional[dict]:
    """``embedding-model.json`` (or ``state_file``, e.g.
    :data:`ROUTER_STATE_FILE`) when the model was last found unavailable,
    else None: ``{"model", "reason", "since", "last_warned"}``."""
    try:
        doc = json.loads(state_path(data_dir, state_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("model") else None


def _write_state(data_dir: Optional[Path], doc: dict, state_file: str = STATE_FILE) -> None:
    from tryaii.auth.store import write_atomic

    prefix = "." + state_file.rsplit(".", 1)[0] + "-"
    write_atomic(state_path(data_dir, state_file), (json.dumps(doc) + "\n").encode("utf-8"),
                 prefix=prefix)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def report_unavailable(model_id: str, reason: str, *, data_dir: Optional[Path] = None,
                       log: Optional[logging.Logger] = None, message: str = "",
                       today: Optional[str] = None, state_file: str = STATE_FILE) -> bool:
    """Record that ``model_id`` is unavailable and log it -- as a warning at
    most once per machine per local day for the same reason (once per process
    when the data dir is not writable), at debug level otherwise. Returns True
    when the warning was logged. Never raises."""
    log = log or logger
    today = today or _dt.date.today().isoformat()
    text = message or f"embedding model {model_id} is not available ({reason})"
    previous = read_state(data_dir, state_file) or {}
    same = previous.get("model") == model_id and previous.get("reason") == reason
    doc = {
        "model": model_id,
        "reason": reason,
        "since": previous.get("since") if same and previous.get("since") else _now_iso(),
        "last_warned": previous.get("last_warned") if same else None,
    }
    warn = doc["last_warned"] != today
    key = (state_file, model_id, reason)
    if warn:
        doc["last_warned"] = today
        try:
            _write_state(data_dir, doc, state_file)
        except OSError:
            # Read-only data dir: still never more than once per process.
            warn = key not in _WARNED_THIS_PROCESS
    elif not same:
        try:
            _write_state(data_dir, doc, state_file)
        except OSError:
            pass
    if warn:
        _WARNED_THIS_PROCESS.add(key)
        log.warning(text)
    else:
        log.debug(text)
    return warn


def clear_unavailable(data_dir: Optional[Path] = None,
                      log: Optional[logging.Logger] = None,
                      state_file: str = STATE_FILE) -> None:
    """The model is available (again): drop the state file, saying so once."""
    path = state_path(data_dir, state_file)
    if not path.exists():
        return
    try:
        path.unlink()
    except OSError:
        return
    if state_file == ROUTER_STATE_FILE:
        (log or logger).info("router model available again; routing with it")
    else:
        (log or logger).info("embedding model available again; classification is back on")


def reset_cache() -> None:
    """Forget verified files and per-process warnings (tests)."""
    with _LOCK:
        _VERIFIED.clear()
    _WARNED_THIS_PROCESS.clear()
    _LAST_ERROR["error"] = None
    _ROUTER_ERROR["error"] = None
