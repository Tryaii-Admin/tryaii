"""The ONNX embedding engine (TS-252): model resolution, the missing-model
report, backend choice, per-model centroids.

The model store is exercised with small stand-in files whose sha256 is
pinned through a patched ``MODELS`` entry and a fake model package, so
nothing here needs the real model file or onnxruntime sessions. The
real-model checks are in test_embedding_parity.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import types

import numpy as np
import pytest

from tryaii.catalog.bundle import starter_bundle
from tryaii.embeddings import model_store
from tryaii.embeddings.model_store import (
    DEFAULT_ONNX_MODEL,
    ENV_MODEL_PATH,
    ModelSpec,
    ModelUnavailableError,
    resolve_model_path,
)

PAYLOAD = b"not really an onnx model, but the right bytes"
MODEL = "test-onnx-model"
PACKAGE = "fake_tryaii_model_pkg"


@pytest.fixture
def fake_model(monkeypatch, tmp_path):
    """A MODELS entry pinned to PAYLOAD, shipped by a fake package whose
    file lives in tmp_path/pkg/model.onnx."""
    spec = ModelSpec(model_id=MODEL, sha256=hashlib.sha256(PAYLOAD).hexdigest(),
                     size_bytes=len(PAYLOAD), dimension=384, max_length=256,
                     base_model="all-MiniLM-L6-v2", package=PACKAGE,
                     distribution="fake-tryaii-model")
    monkeypatch.setitem(model_store.MODELS, MODEL, spec)
    monkeypatch.delenv(ENV_MODEL_PATH, raising=False)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "model.onnx").write_bytes(PAYLOAD)
    module = types.ModuleType(PACKAGE)
    module.model_path = lambda: pkg / "model.onnx"
    monkeypatch.setitem(sys.modules, PACKAGE, module)
    model_store.reset_cache()
    yield pkg / "model.onnx"
    model_store.reset_cache()


# --- the pinned model -------------------------------------------------------

def test_default_model_is_pinned_and_matches_its_package():
    spec = model_store.MODELS[DEFAULT_ONNX_MODEL]
    assert spec.sha256 == "8199be6f699a395f99f89df973f9dc5285fbcfcdb2ae1a4e1201dab014f3d2a6"
    assert spec.size_bytes == 22_928_619
    assert spec.dimension == 384 and spec.max_length == 256
    assert spec.distribution == "tryaii-minilm-int8"
    assert model_store.tokenizer_path(DEFAULT_ONNX_MODEL).is_file()
    pkg = pytest.importorskip(spec.package)
    assert (pkg.MODEL_ID, pkg.SHA256, pkg.SIZE_BYTES) == (
        spec.model_id, spec.sha256, spec.size_bytes)


def test_model_package_is_a_pinned_dependency():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    from tests.conftest import REPO_ROOT

    tryaii_doc = tomllib.loads(
        (REPO_ROOT / "packages" / "python" / "pyproject.toml").read_text(encoding="utf-8"))
    model_doc = tomllib.loads(
        (REPO_ROOT / "packages" / "python-model" / "pyproject.toml").read_text(encoding="utf-8"))
    name, version = model_doc["project"]["name"], model_doc["project"]["version"]
    assert f"{name}=={version}" in tryaii_doc["project"]["dependencies"]
    init = (REPO_ROOT / "packages" / "python-model" / "tryaii_minilm_int8" /
            "__init__.py").read_text(encoding="utf-8")
    assert f'__version__ = "{version}"' in init


# --- resolution -------------------------------------------------------------

def test_the_installed_package_is_used_and_verified(fake_model):
    assert resolve_model_path(MODEL) == fake_model
    assert model_store.last_error() is None


def test_a_missing_package_is_one_reason(fake_model, monkeypatch):
    monkeypatch.setitem(sys.modules, PACKAGE, None)   # import fails
    assert resolve_model_path(MODEL) is None
    assert model_store.last_error() == "package fake-tryaii-model is not installed"


def test_a_tampered_package_file_is_refused(fake_model):
    fake_model.write_bytes(PAYLOAD[:-1] + b"?")
    assert resolve_model_path(MODEL) is None
    assert "does not match" in model_store.last_error()


def test_override_path_wins_and_is_verified(fake_model, tmp_path, monkeypatch):
    placed = tmp_path / "placed.onnx"
    placed.write_bytes(PAYLOAD)
    monkeypatch.setenv(ENV_MODEL_PATH, str(placed))
    assert resolve_model_path(MODEL) == placed
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "model.onnx").write_bytes(PAYLOAD)
    monkeypatch.setenv(ENV_MODEL_PATH, str(tmp_path / "dir"))
    assert resolve_model_path(MODEL) == tmp_path / "dir" / "model.onnx"


def test_a_wrong_override_is_an_error_not_a_silent_fallback(fake_model, tmp_path, monkeypatch):
    placed = tmp_path / "placed.onnx"
    placed.write_bytes(PAYLOAD + b"x")
    monkeypatch.setenv(ENV_MODEL_PATH, str(placed))
    assert resolve_model_path(MODEL) is None         # the package is NOT used instead
    assert ENV_MODEL_PATH in model_store.last_error()


# --- reporting a missing model ----------------------------------------------

def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_report_warns_once_a_day_per_machine(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="tryaii.embeddings")
    for _ in range(3):                               # three processes, same day
        model_store.reset_cache()
        model_store.report_unavailable(MODEL, "package x is not installed",
                                       data_dir=tmp_path, today="2026-10-19")
    assert len(_warnings(caplog)) == 1
    state = model_store.read_state(tmp_path)
    assert state["model"] == MODEL and state["reason"] == "package x is not installed"
    assert state["last_warned"] == "2026-10-19"
    since = state["since"]
    model_store.report_unavailable(MODEL, "package x is not installed",
                                   data_dir=tmp_path, today="2026-10-20")
    assert len(_warnings(caplog)) == 2               # the next day: once more
    assert model_store.read_state(tmp_path)["since"] == since


def test_a_new_reason_warns_again(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="tryaii.embeddings")
    model_store.report_unavailable(MODEL, "a", data_dir=tmp_path, today="2026-10-19")
    model_store.report_unavailable(MODEL, "b", data_dir=tmp_path, today="2026-10-19")
    assert len(_warnings(caplog)) == 2
    assert model_store.read_state(tmp_path)["reason"] == "b"


def test_read_only_data_dir_still_warns_once_per_process(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="tryaii.embeddings")
    model_store.reset_cache()

    def read_only(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(model_store, "_write_state", read_only)
    for _ in range(3):
        model_store.report_unavailable(MODEL, "a", data_dir=tmp_path, today="2026-10-19")
    assert len(_warnings(caplog)) == 1


def test_clear_drops_the_state(tmp_path):
    model_store.report_unavailable(MODEL, "a", data_dir=tmp_path, today="2026-10-19")
    assert model_store.read_state(tmp_path) is not None
    model_store.clear_unavailable(tmp_path)
    assert model_store.read_state(tmp_path) is None
    model_store.clear_unavailable(tmp_path)          # idempotent


# --- providers --------------------------------------------------------------

def test_local_provider_picks_the_backend_by_model_name():
    from tryaii.embeddings import (
        LocalEmbeddingProvider,
        OnnxEmbeddingProvider,
        SentenceTransformerEmbeddingProvider,
    )

    default = LocalEmbeddingProvider()
    assert isinstance(default.backend, OnnxEmbeddingProvider)
    assert default.model_name == DEFAULT_ONNX_MODEL and default.dimension == 384
    fp32 = LocalEmbeddingProvider("all-MiniLM-L6-v2")
    assert isinstance(fp32.backend, SentenceTransformerEmbeddingProvider)
    assert fp32.model_name == "all-MiniLM-L6-v2"


def test_onnx_provider_without_a_model_raises_unavailable(monkeypatch):
    from tryaii.embeddings.onnx_provider import OnnxEmbeddingProvider

    spec = model_store.MODELS[DEFAULT_ONNX_MODEL]
    monkeypatch.delenv(ENV_MODEL_PATH, raising=False)
    monkeypatch.setitem(sys.modules, spec.package, None)
    model_store.reset_cache()
    with pytest.raises(ModelUnavailableError,
                       match="pip install --force-reinstall tryaii-minilm-int8"):
        OnnxEmbeddingProvider(DEFAULT_ONNX_MODEL).embed("hello")


def test_base_install_does_not_need_torch():
    """sentence-transformers is the optional ``centroids`` extra."""
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    from tests.conftest import REPO_ROOT

    doc = tomllib.loads(
        (REPO_ROOT / "packages" / "python" / "pyproject.toml").read_text(encoding="utf-8"))
    deps = " ".join(doc["project"]["dependencies"]).lower()
    assert "sentence-transformers" not in deps and "torch" not in deps
    assert "onnxruntime" in deps and "tokenizers" in deps
    assert any("sentence-transformers" in d
               for d in doc["project"]["optional-dependencies"]["centroids"])


# --- centroids per embedding model -------------------------------------------

class NamedProvider:
    """Just enough provider for the centroid loader's validation."""

    def __init__(self, name: str, dim: int = 384):
        self.model_name = name
        self.dimension = dim

    def embed_batch(self, texts):  # pragma: no cover - must not be called
        raise AssertionError("no generation expected")


def test_starter_bundle_carries_both_models():
    bundle = starter_bundle()
    assert bundle.embedding_model == "all-MiniLM-L6-v2"
    assert bundle.embedding_models == ["all-MiniLM-L6-v2", "all-MiniLM-L6-v2-onnx-int8"]
    fp32 = bundle.centroids_for("all-MiniLM-L6-v2")
    int8 = bundle.centroids_for("all-MiniLM-L6-v2-onnx-int8")
    assert fp32 is bundle.centroids
    assert int8["metadata"]["model"] == "all-MiniLM-L6-v2-onnx-int8"
    assert set(int8["centroids"]) == set(fp32["centroids"])
    assert int8["centroids"] != fp32["centroids"]
    assert bundle.centroids_for("text-embedding-3-small") is None


@pytest.mark.parametrize("model", ["all-MiniLM-L6-v2", "all-MiniLM-L6-v2-onnx-int8"])
def test_loader_scores_a_model_only_with_its_own_centroids(model, tmp_path):
    from tryaii.centroids.loader import CentroidLoader
    from tryaii.config import TryaiiDreConfig

    bundle = starter_bundle()
    loader = CentroidLoader(TryaiiDreConfig(embedding_model=model, data_dir=tmp_path),
                            NamedProvider(model), bundle=bundle)
    centroids = loader.get_centroids()
    expected = bundle.centroids_for(model)["centroids"]
    name = next(iter(expected))
    assert np.allclose(centroids[name], np.array(expected[name], dtype=np.float32))


def test_loader_never_borrows_another_models_centroids(tmp_path):
    from tryaii.centroids.loader import CentroidLoader
    from tryaii.config import TryaiiDreConfig

    loader = CentroidLoader(TryaiiDreConfig(embedding_model="other", data_dir=tmp_path),
                            NamedProvider("other"), bundle=starter_bundle())
    assert loader._try_bundle() is None


def test_bundle_rejects_mislabelled_model_centroids(tmp_path):
    import shutil

    from tryaii.catalog.bundle import (
        STARTER_BUNDLE_DIR,
        BundleError,
        canonical_json,
        load_bundle,
        sha256_hex,
    )

    copy = tmp_path / "bundle"
    shutil.copytree(STARTER_BUNDLE_DIR, copy)
    name = "centroids.all-MiniLM-L6-v2-onnx-int8.json"
    doc = json.loads((copy / name).read_text(encoding="utf-8"))
    doc["metadata"]["model"] = "all-MiniLM-L6-v2"
    text = canonical_json(doc)
    (copy / name).write_bytes(text.encode("utf-8"))  # exact bytes (3.9: no newline=)
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][name] = sha256_hex(text.encode("utf-8"))
    (copy / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleError, match="was built for"):
        load_bundle(copy)


def test_model_centroid_file_names():
    from tryaii.catalog.bundle import model_centroid_files, model_centroids_file

    assert model_centroids_file("all-MiniLM-L6-v2-onnx-int8") == (
        "centroids.all-MiniLM-L6-v2-onnx-int8.json")
    with pytest.raises(ValueError):
        model_centroids_file("../evil")
    assert model_centroid_files({"centroids.json": "x", "centroids.b.json": "y",
                                 "centroids.a.json": "z", "models.json": "w",
                                 "centroids..json": "v"}) == (
        "centroids.a.json", "centroids.b.json")
