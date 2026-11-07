"""The H4 router: static provider, head, payload integrity, router selection
and fallback, monitor/daemon wiring.

Uses the installed ``tryaii-router-h4`` payload (a dependency of tryaii); the
cross-SDK cases live in shared/router/h4_cases.json (scripts/gen-h4-fixtures.py).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from tryaii.classifiers.head import HeadClassifier, RouterHead, softmax
from tryaii.config import (
    DEFAULT_ROUTER,
    ROUTER_H4,
    ROUTER_MINILM,
    TryaiiDreConfig,
    normalize_router,
)
from tryaii.embeddings import model_store
from tryaii.embeddings.static_provider import StaticEmbeddingProvider, load_safetensors

REPO = Path(__file__).resolve().parents[3]
CASES = REPO / "shared" / "router" / "h4_cases.json"
SPEC = model_store.ROUTERS[model_store.DEFAULT_ROUTER_MODEL]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("TRYAII_ROUTER", raising=False)
    monkeypatch.delenv(model_store.ENV_ROUTER_MODEL_PATH, raising=False)
    monkeypatch.setenv("TRYAII_DRE_DATA_DIR", str(tmp_path / "data"))
    model_store.reset_cache()
    yield
    model_store.reset_cache()


@pytest.fixture(scope="module")
def provider():
    p = StaticEmbeddingProvider()
    p.ensure_loaded()
    return p


@pytest.fixture(scope="module")
def head(provider):
    return RouterHead.from_dir(provider.directory)


def _payload_copy(tmp_path: Path) -> Path:
    src = model_store.resolve_router_dir()
    assert src is not None
    dst = tmp_path / "h4"
    dst.mkdir()
    for name in SPEC.files:
        shutil.copyfile(src / name, dst / name)
    return dst


# -- payload integrity -----------------------------------------------------------

class TestPayload:
    def test_installed_package_matches_the_pin(self):
        import tryaii_router_h4 as pkg

        assert pkg.MODEL_ID == SPEC.router_id
        assert pkg.SHA256 == SPEC.sha256
        assert pkg.SIZE_BYTES == SPEC.size_bytes
        assert tuple(pkg.FILES) == SPEC.files
        digest, size = model_store.payload_sha256(pkg.model_dir(), SPEC.files)
        assert (digest, size) == (SPEC.sha256, SPEC.size_bytes)

    def test_sha256sums_file_is_the_hashed_manifest(self):
        import hashlib

        import tryaii_router_h4 as pkg

        text = (Path(pkg.model_dir()) / "SHA256SUMS").read_text(encoding="utf-8")
        assert text == model_store.payload_manifest(pkg.model_dir(), SPEC.files)[0]
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == SPEC.sha256

    def test_resolves_the_package(self):
        path = model_store.resolve_router_dir()
        assert path is not None and (path / "vectors.safetensors").is_file()
        assert model_store.last_router_error() is None

    def test_override_with_a_good_copy(self, tmp_path, monkeypatch):
        copy = _payload_copy(tmp_path)
        monkeypatch.setenv(model_store.ENV_ROUTER_MODEL_PATH, str(copy))
        assert model_store.resolve_router_dir() == copy

    def test_override_with_a_tampered_copy(self, tmp_path, monkeypatch):
        copy = _payload_copy(tmp_path)
        meta = copy / "meta.json"
        data = bytearray(meta.read_bytes())
        data[-2] = ord(" ")  # same size, different bytes
        meta.write_bytes(bytes(data))
        monkeypatch.setenv(model_store.ENV_ROUTER_MODEL_PATH, str(copy))
        assert model_store.resolve_router_dir() is None
        assert model_store.ENV_ROUTER_MODEL_PATH in model_store.last_router_error()

    def test_override_missing_file(self, tmp_path, monkeypatch):
        copy = _payload_copy(tmp_path)
        (copy / "head.safetensors").unlink()
        monkeypatch.setenv(model_store.ENV_ROUTER_MODEL_PATH, str(copy))
        assert model_store.resolve_router_dir() is None

    def test_package_not_installed(self, monkeypatch):
        monkeypatch.setattr(model_store, "package_router_dir", lambda spec: None)
        assert model_store.resolve_router_dir() is None
        assert "not installed" in model_store.last_router_error()

    def test_unknown_router(self):
        assert model_store.resolve_router_dir("nope") is None

    def test_safetensors_reader(self, provider):
        t = load_safetensors(provider.directory / "head.safetensors")
        assert sorted(t) == ["W0", "W1", "b0", "b1"]
        assert t["W0"].shape == (512, 256) and t["W0"].dtype == np.float32
        v = load_safetensors(provider.directory / "vectors.safetensors")["embeddings"]
        assert v.shape == (40000, 256) and v.dtype == np.int8

    def test_meta(self, provider):
        meta = provider.meta
        assert meta["router_id"] == "h4-he-en" and meta["version"] == "1.0.0"
        assert meta["temperature"] == 2.0 and meta["max_tokens"] == 700
        assert meta["dim"] == 256 and meta["normalizer"] == "nfkc"
        assert meta["char_cap"] == meta["max_tokens"] * meta["median_token_length"]


# -- static provider -------------------------------------------------------------

class TestStaticProvider:
    def test_unit_vector(self, provider):
        v = provider.embed("Write a haiku about autumn leaves.")
        assert v.dtype == np.float32 and v.shape == (256,)
        assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-5

    def test_deterministic_and_batch_equals_single(self, provider):
        texts = ["first prompt", "שלום עולם", "third one"]
        batch = provider.embed_batch(texts)
        for t, b in zip(texts, batch):
            assert np.array_equal(provider.embed(t), b)
            assert np.array_equal(provider.embed(t), provider.embed(t))

    def test_empty_text_is_zero(self, provider):
        assert provider.token_ids("") == []
        assert not provider.embed("").any()

    def test_mean_of_int8_rows(self, provider):
        text = "Mean pooling over the int8 token vectors."
        ids = provider.token_ids(text)
        rows = provider._vectors[ids].astype(np.float64)
        mean = rows.mean(axis=0)
        expected = mean / np.linalg.norm(mean)
        assert np.abs(provider.embed(text) - expected).max() < 1e-6

    def test_char_cap(self, provider):
        cap = provider.meta["char_cap"]
        long_text = "word " * 2000
        assert provider.token_ids(long_text) == provider.token_ids(long_text[:cap])

    def test_token_cap(self, provider):
        ids = provider.token_ids("a b c d e f g " * 400)
        assert len(ids) <= provider.meta["max_tokens"]

    def test_unknown_token_is_dropped(self, provider):
        assert provider.meta["unk_id"] not in provider.token_ids("\U0001f600 \u2603 \u0b85")

    def test_no_special_tokens(self, provider):
        ids = provider.token_ids("hello")
        assert 0 not in ids and len(ids) >= 1

    def test_dimension_and_name(self):
        p = StaticEmbeddingProvider()
        assert p.dimension == 256 and p.model_name == "h4-he-en"

    def test_unknown_model(self):
        with pytest.raises(ValueError):
            StaticEmbeddingProvider("nope")

    def test_unavailable(self, monkeypatch):
        monkeypatch.setattr(model_store, "package_router_dir", lambda spec: None)
        import tryaii.embeddings.static_provider as sp

        monkeypatch.setattr(sp, "resolve_router_dir", model_store.resolve_router_dir)
        with pytest.raises(model_store.RouterUnavailableError):
            StaticEmbeddingProvider().ensure_loaded()


# -- head --------------------------------------------------------------------------

class TestHead:
    def test_softmax_temperature(self):
        p = softmax(np.array([2.0, 0.0]), 2.0)
        assert p[0] == pytest.approx(np.e / (np.e + 1))
        assert softmax(np.array([1000.0, 1000.0]), 2.0) == pytest.approx([0.5, 0.5])

    def test_tiny_head_math(self):
        w0 = np.array([[1.0, -1.0], [0.5, 0.5]], np.float32)
        b0 = np.array([0.0, -1.0], np.float32)
        w1 = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]], np.float32)
        b1 = np.array([0.0, 0.0, -1.0], np.float32)
        h = RouterHead([(w0, b0), (w1, b1)], ["a", "b", "c"], 2.0)
        e = np.array([1.0, 0.0])
        hidden = np.maximum(w0 @ e + b0, 0)          # [1, 0] (relu clips -0.5)
        logits = w1 @ hidden + b1                    # [1, 0, 0]
        assert h.logits(e) == pytest.approx(logits)
        assert h.probabilities(e) == pytest.approx(softmax(logits, 2.0))
        assert sum(h.scores(e).values()) == pytest.approx(1.0)

    def test_class_mismatch(self):
        w = np.zeros((2, 2), np.float32)
        with pytest.raises(ValueError):
            RouterHead([(w, np.zeros(2, np.float32))], ["only-one"], 2.0)

    def test_mask_renormalises(self, provider, head):
        e = provider.embed("Prove that the square root of 2 is irrational.")
        full = head.scores(e)
        keep = {"GPQA", "MATH", "HLE", "not-a-class"}
        masked = head.scores(e, keep)
        assert set(masked) == {"GPQA", "MATH", "HLE"}
        assert sum(masked.values()) == pytest.approx(1.0, abs=1e-12)
        total = sum(full[n] for n in masked)
        for n, v in masked.items():
            assert v == pytest.approx(full[n] / total, rel=1e-9)
        assert head.scores(e, set()) == {}
        assert head.scores(e, set(head.classes)) == pytest.approx(full)

    def test_payload_head(self, head):
        assert len(head.classes) == 33 and head.temperature == 2.0
        assert head.classes == sorted(head.classes)  # POC classes were 0..32 in order

    def test_scores_sum_to_one(self, provider, head):
        s = head.scores(provider.embed("Prove that the square root of 2 is irrational."))
        assert len(s) == 33 and sum(s.values()) == pytest.approx(1.0)


# -- cross-SDK cases -------------------------------------------------------------

def _fixture():
    return json.loads(CASES.read_text(encoding="utf-8"))


def _cases():
    return _fixture()["cases"]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_shared_h4_cases(case, provider, head):
    exp = case["expected"]
    assert provider.token_ids(case["text"]) == exp["token_ids"]
    p = head.probabilities(provider.embed(case["text"]))
    got = dict(zip(head.classes, p))
    assert set(got) == set(exp["probabilities"])
    for name, value in exp["probabilities"].items():
        assert abs(got[name] - value) <= 1e-6, name
    order = sorted(got, key=lambda n: (-got[n], n))
    assert order[:3] == exp["top3"]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_shared_h4_cases_masked(case, provider):
    keep = _fixture()["mask"]["keep"]
    exp = case["expected"]["masked"]
    clf = HeadClassifier(provider, catalog_benchmarks=keep)
    r = clf.classify(case["text"]) if case["text"] else None
    got = (r.benchmark_scores if r is not None
           else clf.head.scores(provider.embed(""), set(keep)))
    assert set(got) == set(exp["probabilities"]) == set(keep)
    for name, value in exp["probabilities"].items():
        assert abs(got[name] - value) <= 1e-6, name
    assert sorted(got, key=lambda n: (-got[n], n))[:3] == exp["top3"]
    if r is not None:
        assert r.masked_classes == exp["masked_classes"]
        assert r.confidence == got[exp["top3"][0]]


# -- classifier ------------------------------------------------------------------

class TestHeadClassifier:
    def test_classify(self, provider):
        clf = HeadClassifier(provider).load()
        r = clf.classify("Fix the segfault in this C program.")
        assert r.classifier_used == "head"
        assert len(r.benchmark_scores) == 33
        assert r.confidence == max(r.benchmark_scores.values())
        assert 0.0 < r.difficulty < 1.0
        assert r.broad_category and r.subcategory

    def test_cache_hit(self, provider):
        clf = HeadClassifier(provider)
        first = clf.classify("cache me")
        second = clf.classify("cache me")
        assert not first.cache_hit and second.cache_hit
        assert first.benchmark_scores == second.benchmark_scores

    def test_no_catalog_keeps_every_class(self, provider):
        clf = HeadClassifier(provider)
        assert clf.masked_classes == 0
        assert clf.classify("hello").masked_classes == 0

    def test_masked_to_the_catalog(self, provider):
        keep = ["GPQA", "HLE", "Chatbot Arena Elo", "SWE-bench-Pro"]
        clf = HeadClassifier(provider, catalog_benchmarks=keep + ["Custom"])
        assert clf.kept_classes == sorted(keep) and clf.masked_classes == 29
        r = clf.classify("Fix the segfault in this C program.")
        assert set(r.benchmark_scores) == set(keep)
        assert sum(r.benchmark_scores.values()) == pytest.approx(1.0)
        assert r.masked_classes == 29
        assert r.confidence == max(r.benchmark_scores.values())

    def test_bundle_masks(self, provider):
        from tryaii.catalog import resolve_bundle

        bundle = resolve_bundle(None, catalog="starter")
        clf = HeadClassifier(provider, bundle=bundle)
        names = set(bundle.benchmark_names)
        assert set(clf.kept_classes) == names & set(clf.head.classes)
        r = clf.classify("Summarise this contract clause.")
        assert set(r.benchmark_scores) <= names
        assert r.masked_classes == 33 - len(r.benchmark_scores)

    def test_all_masked_raises(self, provider):
        clf = HeadClassifier(provider, catalog_benchmarks=["Custom"])
        assert clf.kept_classes == []
        with pytest.raises(ValueError):
            clf.classify("anything")

    def test_difficulty_orders_easy_below_hard(self, provider):
        clf = HeadClassifier(provider)
        easy = clf.classify("What is the capital of France?").difficulty
        hard = clf.classify("Design a distributed consensus protocol tolerant to Byzantine "
                            "faults and prove its safety and liveness.").difficulty
        assert easy < hard


# -- selection + fallback ----------------------------------------------------------

def _fake_onnx(monkeypatch):
    from tryaii.embeddings.onnx_provider import OnnxEmbeddingProvider

    def encode(self, texts):
        out = []
        for t in texts:
            rng = np.random.RandomState(sum(t.encode("utf-8")) % (2**32))
            v = rng.randn(384).astype(np.float32)
            out.append(v / np.linalg.norm(v))
        return np.stack(out)

    monkeypatch.setattr(OnnxEmbeddingProvider, "_encode", encode)


class TestSelection:
    def test_normalize_router(self):
        assert DEFAULT_ROUTER == ROUTER_H4
        assert normalize_router(None) == ROUTER_H4
        assert normalize_router("") == ROUTER_H4
        assert normalize_router(" H4 ") == ROUTER_H4
        assert normalize_router("h4-he-en") == ROUTER_H4
        assert normalize_router("minilm") == ROUTER_MINILM
        assert normalize_router("MiniLM") == ROUTER_MINILM
        assert normalize_router("all-MiniLM-L6-v2-onnx-int8") == ROUTER_MINILM
        assert normalize_router("bogus") == ROUTER_H4

    def test_config_reads_env(self, monkeypatch):
        assert TryaiiDreConfig().router == ROUTER_H4
        monkeypatch.setenv("TRYAII_ROUTER", "minilm")
        assert TryaiiDreConfig().router == ROUTER_MINILM
        assert not TryaiiDreConfig().uses_h4

    def test_custom_embedding_model_disables_h4(self):
        assert not TryaiiDreConfig(embedding_model="all-MiniLM-L6-v2").uses_h4
        assert TryaiiDreConfig().uses_h4

    def test_router_default_is_h4(self):
        from tryaii import Router

        router = Router(catalog="starter", policy=None)
        result = router.route("Refactor this JavaScript class into hooks.")
        assert result.classification.classifier_used == "head"
        assert router.router_id == "h4-he-en"
        # The loaded payload's meta.json version, on the result and the wire.
        version = router._classifier.provider.meta["version"]
        assert result.classification.router == f"h4-he-en@{version}" == router.router_tag
        from tryaii.server import serialize_route_result
        assert serialize_route_result(result)["classification"]["router"] == router.router_tag
        assert result.best_model
        assert len(router.embed_prompt("x")) == 256
        # Masked to the starter catalog's benchmarks, renormalised.
        names = set(router._bundle.benchmark_names)
        scores = result.classification.benchmark_scores
        assert set(scores) <= names
        assert sum(scores.values()) == pytest.approx(1.0)
        assert result.classification.masked_classes == 33 - len(scores) > 0

    def test_router_falls_back_when_catalog_has_no_class(self, monkeypatch, caplog):
        from tryaii import Router
        from tryaii.classifiers import selection
        from tryaii.classifiers.embedding import EmbeddingClassifier

        _fake_onnx(monkeypatch)
        monkeypatch.setattr(selection, "_WARNED_NO_CLASSES", set())
        router = Router(catalog="starter", policy=None)
        monkeypatch.setattr(type(router._bundle), "benchmark_names",
                            property(lambda self: ["Only-Custom"]))
        with caplog.at_level("WARNING", logger="tryaii"):
            result = router.route("Summarise this paragraph.")
        assert isinstance(router._classifier, EmbeddingClassifier)
        assert result.classification.classifier_used == "embedding"
        assert any("none of the benchmarks the router model h4-he-en scores" in r.getMessage()
                   for r in caplog.records)

    def test_router_minilm_by_env(self, monkeypatch):
        from tryaii import Router
        from tryaii.classifiers.embedding import EmbeddingClassifier

        _fake_onnx(monkeypatch)
        monkeypatch.setenv("TRYAII_ROUTER", "minilm")
        router = Router(catalog="starter", policy=None)
        result = router.route("Refactor this JavaScript class into hooks.")
        assert isinstance(router._classifier, EmbeddingClassifier)
        assert result.classification.classifier_used == "embedding"
        assert router.router_id == model_store.DEFAULT_ONNX_MODEL
        assert result.classification.router == f"minilm@{model_store.DEFAULT_ONNX_MODEL}"

    def test_fallback_when_h4_broken(self, monkeypatch, tmp_path, caplog):
        from tryaii import Router
        from tryaii.classifiers.embedding import EmbeddingClassifier

        _fake_onnx(monkeypatch)
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv(model_store.ENV_ROUTER_MODEL_PATH, str(empty))
        config = TryaiiDreConfig(data_dir=tmp_path / "data")
        router = Router(config=config, catalog="starter", policy=None)
        with caplog.at_level("WARNING", logger="tryaii"):
            result = router.route("Summarise this paragraph.")
        assert isinstance(router._classifier, EmbeddingClassifier)
        assert result.best_model
        state = model_store.read_state(config.data_dir, model_store.ROUTER_STATE_FILE)
        assert state and state["model"] == "h4-he-en"
        assert any("h4-he-en is not available" in r.getMessage() for r in caplog.records)
        # MiniLM classification is not "off": its own state file is untouched.
        assert model_store.read_state(config.data_dir) is None

    def test_fallback_when_payload_load_fails(self, monkeypatch, tmp_path):
        from tryaii import Router
        from tryaii.classifiers.embedding import EmbeddingClassifier

        _fake_onnx(monkeypatch)

        def boom(self):
            raise ValueError("corrupt")

        monkeypatch.setattr(StaticEmbeddingProvider, "ensure_loaded", boom)
        router = Router(config=TryaiiDreConfig(data_dir=tmp_path / "d"), catalog="starter",
                        policy=None)
        router.route("anything")
        assert isinstance(router._classifier, EmbeddingClassifier)

    def test_state_cleared_when_h4_back(self, tmp_path):
        from tryaii.classifiers.selection import h4_ready, record_h4_unavailable

        config = TryaiiDreConfig(data_dir=tmp_path / "d")
        record_h4_unavailable(config, "test")
        assert model_store.read_state(config.data_dir, model_store.ROUTER_STATE_FILE)
        assert h4_ready(config)
        assert model_store.read_state(config.data_dir, model_store.ROUTER_STATE_FILE) is None

    def test_custom_provider_means_centroids(self, monkeypatch):
        from tryaii import Router
        from tryaii.classifiers.embedding import EmbeddingClassifier
        from tryaii.embeddings.onnx_provider import OnnxEmbeddingProvider

        _fake_onnx(monkeypatch)
        router = Router(catalog="starter", policy=None,
                        embedding_provider=OnnxEmbeddingProvider())
        router.route("hello there")
        assert isinstance(router._classifier, EmbeddingClassifier)

    def test_add_benchmark_warns_under_h4(self, caplog):
        from tryaii import Router

        router = Router(catalog="starter", policy=None)
        router.route("warm up")
        with caplog.at_level("WARNING", logger="tryaii"):
            router.add_benchmark("CustomQA", ["q1", "q2"])
        assert any("not scored by the H4 router" in r.getMessage() for r in caplog.records)


# -- monitor / daemon wiring -----------------------------------------------------

class TestWiring:
    def test_transport_ready_with_h4_skips_minilm(self, monkeypatch, tmp_path):
        from tryaii.monitor.transport import Transport

        called = []
        monkeypatch.setattr(model_store, "resolve_model_path",
                            lambda m: called.append(m) or None)
        t = Transport.__new__(Transport)
        t._config = TryaiiDreConfig(data_dir=tmp_path)
        assert t._embedding_model_ready() is True
        assert called == []

    def test_transport_falls_back_to_minilm_check(self, monkeypatch, tmp_path):
        from tryaii.monitor.transport import Transport

        monkeypatch.setattr(model_store, "package_router_dir", lambda spec: None)
        called = []
        monkeypatch.setattr(model_store, "resolve_model_path",
                            lambda m: called.append(m) or Path("x"))
        t = Transport.__new__(Transport)
        t._config = TryaiiDreConfig(data_dir=tmp_path)
        assert t._embedding_model_ready() is True
        assert called == [model_store.DEFAULT_ONNX_MODEL]
        assert model_store.read_state(tmp_path, model_store.ROUTER_STATE_FILE)

    def test_status_router_line(self, tmp_path):
        from tryaii.classifiers.selection import record_h4_unavailable
        from tryaii.cli.monitor import _router_line

        assert _router_line(tmp_path) is None
        record_h4_unavailable(TryaiiDreConfig(data_dir=tmp_path), "package missing")
        line = _router_line(tmp_path)
        assert line.startswith("router: h4-he-en unavailable since ")
        assert "(package missing); routing with all-MiniLM-L6-v2-onnx-int8" in line

    def test_daemon_router_must_match(self, monkeypatch, tmp_path):
        from tryaii import daemon

        config = TryaiiDreConfig(data_dir=tmp_path)
        state = {"runtime": daemon.RUNTIME, "embeddingModel": config.embedding_model}
        monkeypatch.setattr(daemon, "read_state", lambda c: dict(state))
        monkeypatch.setattr(daemon, "_request", lambda s, req, timeout: {"ok": True})
        # A pre-H4 daemon (no "router") routes with MiniLM: not reused under H4.
        assert daemon._live_state(config) is None
        state["router"] = "h4"
        assert daemon._live_state(config) is not None
        config.router = "minilm"
        assert daemon._live_state(config) is None
