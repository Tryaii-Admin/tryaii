"""fp32 vs int8 parity of the classification (TS-252).

The parity prompts -- the monitor fixture corpus (the routing texts of
shared/monitor/fixtures/extract) and the catalog parity set -- are classified
with both embedding builds, each scored only with its own centroids:

* fp32: sentence-transformers all-MiniLM-L6-v2 + ``centroids.json``, frozen in
  shared/catalog/parity/embedding_parity.json (scripts/gen-embedding-parity.py)
  so this test needs no PyTorch;
* int8: the ONNX build on onnxruntime + ``centroids.all-MiniLM-L6-v2-onnx-int8.json``,
  run live through the production classifier.

Rule (the ticket's): the top benchmark is identical; the top two may swap
only where both builds put them less than 0.01 apart.

Uses the real model file: ``TRYAII_EMBEDDING_MODEL_PATH``, the installed
``tryaii-minilm-int8`` package, or its source in packages/python-model.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from tests.conftest import REPO_ROOT
from tryaii.embeddings import model_store

INT8 = "all-MiniLM-L6-v2-onnx-int8"
FP32 = "all-MiniLM-L6-v2"
PARITY = REPO_ROOT / "shared" / "catalog" / "parity" / "embedding_parity.json"
SWAP_GAP = 0.01

#: Prompts allowed to diverge beyond the swap rule. Empty: with weight-only
#: int8 (fp32 arithmetic) every parity prompt keeps its top benchmark. The
#: earlier dynamic-int8 build flipped 3 near-ties here, and different ones on
#: other CPUs / onnxruntime versions, which is why it was replaced.
KNOWN_DIVERGENCES: set = set()


def _model_path() -> Path:
    """The verified int8 model: TRYAII_EMBEDDING_MODEL_PATH or the installed
    tryaii-minilm-int8 package, else the repository's model package source."""
    path = model_store.resolve_model_path(INT8)
    if path is not None:
        return path
    spec = model_store.MODELS[INT8]
    source = REPO_ROOT / "packages" / "python-model" / spec.package / model_store.MODEL_FILE
    if source.is_file() and model_store.sha256_file(source) == spec.sha256:
        return source
    pytest.skip(f"int8 ONNX model not available ({model_store.last_error()})")


@pytest.fixture(scope="module")
def parity_doc():
    if not PARITY.is_file():
        pytest.skip("shared/catalog/parity not present (package-only checkout)")
    return json.loads(PARITY.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def int8_classifier(tmp_path_factory):
    from tryaii.catalog.bundle import starter_bundle
    from tryaii.centroids.loader import CentroidLoader
    from tryaii.classifiers.embedding import EmbeddingClassifier
    from tryaii.config import TryaiiDreConfig
    from tryaii.embeddings.onnx_provider import OnnxEmbeddingProvider

    provider = OnnxEmbeddingProvider(INT8, model_path=_model_path())
    config = TryaiiDreConfig(embedding_model=INT8,
                             data_dir=tmp_path_factory.mktemp("parity-data"))
    loader = CentroidLoader(config, provider, bundle=starter_bundle())
    return EmbeddingClassifier(provider, loader, config)


def _ranked(scores: dict) -> list:
    return sorted(scores, key=lambda name: (-scores[name], name))


def _verdict(fp32: dict, int8: dict) -> tuple[bool, str]:
    r32, r8 = _ranked(fp32), _ranked(int8)
    if r32[0] == r8[0]:
        return True, "same"
    swapped = r8[0] == r32[1] and r8[1] == r32[0]
    gap32 = fp32[r32[0]] - fp32[r32[1]]
    gap8 = int8[r8[0]] - int8[r8[1]]
    detail = (f"fp32 {r32[0]} {fp32[r32[0]]:.4f} / {r32[1]} {fp32[r32[1]]:.4f}; "
              f"int8 {r8[0]} {int8[r8[0]]:.4f} / {r8[1]} {int8[r8[1]]:.4f}")
    return swapped and gap32 < SWAP_GAP and gap8 < SWAP_GAP, detail


def test_parity_file_covers_the_fixture_corpus(parity_doc):
    extract = json.loads((REPO_ROOT / "shared" / "monitor" / "fixtures" / "extract" /
                          "cases.json").read_text(encoding="utf-8"))
    texts = [c["expected"]["text"] for c in extract["cases"] if c["expected"]["text"]]
    frozen = [p["prompt"] for p in parity_doc["prompts"]
              if p["source"].startswith("monitor/")]
    assert frozen == texts, "run scripts/gen-embedding-parity.py"
    assert parity_doc["int8_model"] == INT8 and parity_doc["fp32_model"] == FP32


def test_int8_classification_matches_fp32(parity_doc, int8_classifier):
    from tryaii.catalog.bundle import starter_bundle

    categories = starter_bundle().benchmark_categories()
    failures, swaps, known = [], [], []
    for entry in parity_doc["prompts"]:
        fp32 = entry["fp32_similarities"]
        result = int8_classifier.classify(entry["prompt"])
        int8 = result.benchmark_scores
        assert set(int8) == set(fp32)
        ok, detail = _verdict(fp32, int8)
        label = f"{entry['source']}: {entry['prompt'][:60]!r}: {detail}"
        if detail == "same":
            assert result.broad_category == categories[_ranked(fp32)[0]][0]
        elif ok:
            swaps.append(label)
        elif any(entry["prompt"].startswith(p) for p in KNOWN_DIVERGENCES):
            assert entry["source"].startswith("catalog/"), "the fixture corpus has no exceptions"
            known.append(label)
        else:
            failures.append(label)
    assert not failures, "int8 classification diverges from fp32:\n" + "\n".join(failures)


def test_frozen_fp32_side_matches_a_live_run(parity_doc):
    """Only with the ``centroids`` extra installed: the frozen fp32
    similarities are what sentence-transformers produces today."""
    pytest.importorskip("sentence_transformers")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gen_embedding_parity", REPO_ROOT / "scripts" / "gen-embedding-parity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    live = module.fp32_similarities([p["prompt"] for p in parity_doc["prompts"]])
    for entry, sims in zip(parity_doc["prompts"], live):
        for name, value in sims.items():
            assert abs(entry["fp32_similarities"][name] - value) < 1e-5, (entry["prompt"], name)


def test_int8_embeddings_stay_close_to_fp32(parity_doc):
    """Embedding-level parity (cosine >= 0.998 per prompt) -- only with the
    ``centroids`` extra installed."""
    pytest.importorskip("sentence_transformers")
    from tryaii.embeddings.onnx_provider import OnnxEmbeddingProvider
    from tryaii.embeddings.sentence_transformers_provider import (
        SentenceTransformerEmbeddingProvider,
    )

    prompts = [p["prompt"] for p in parity_doc["prompts"]]
    fp32 = np.stack(SentenceTransformerEmbeddingProvider(FP32, device="cpu").embed_batch(prompts))
    int8 = np.stack(OnnxEmbeddingProvider(INT8, model_path=_model_path()).embed_batch(prompts))
    cos = (fp32 * int8).sum(axis=1)
    assert float(cos.min()) >= 0.998, float(cos.min())
