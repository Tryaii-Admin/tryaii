"""Catalog bundles: loader integrity, the starter selection, and routing parity.

Contract: docs/catalog/CONTRACT-catalog-v1.md (section 1 bundle format,
section 2 building, Appendix A benchmarks.json).

* loader -- sha256 of every data file's exact bytes against the manifest,
  schema gate, required files, cross-file benchmark consistency;
* starter selection -- shared/catalog/starter is exactly what
  scripts/build-catalog-bundles.py builds from the committed inputs;
* engine parity -- Python and Node score the fixed starter prompt set
  (shared/catalog/parity/starter_prompts.json) identically to 1e-9;
* full-catalog regression -- routing on build/catalog/full reproduces the picks
  captured with the pre-bundle engine (shared/catalog/parity/full_baseline_routes.json).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tryaii import Router
from tryaii.benchmarks.registry import BenchmarkRegistry
from tryaii.catalog import (
    BUNDLE_DATA_FILES,
    SUPPORTED_SCHEMA,
    BundleError,
    BundleIntegrityError,
    BundleSchemaError,
    CatalogBundle,
    bundle_from_texts,
    canonical_json,
    load_bundle,
    resolve_bundle,
    starter_bundle,
)
from tryaii.catalog.bundle import STARTER_BUNDLE_DIR
from tryaii.registry.models import ModelRegistry
from tryaii.scoring.engine import ScoringEngine
from tryaii.scoring.priorities import Priorities

from .conftest import FULL_BUNDLE_DIR, REPO_ROOT, load_full_bundle

PARITY = REPO_ROOT / "shared" / "catalog" / "parity"
NODE_DIST_INDEX = REPO_ROOT / "packages" / "node" / "dist" / "index.js"
NODE_EXE = shutil.which("node")


@pytest.fixture
def bundle_copy(tmp_path) -> Path:
    """A writable copy of the packaged starter bundle."""
    dest = tmp_path / "bundle"
    shutil.copytree(STARTER_BUNDLE_DIR, dest)
    return dest


def _manifest(directory: Path) -> dict:
    return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))


def _write_manifest(directory: Path, manifest: dict) -> None:
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


# ------------------------------------------------------------------- loader
class TestLoader:
    def test_starter_bundle_loads_and_is_a_starter(self):
        bundle = starter_bundle()
        assert isinstance(bundle, CatalogBundle)
        assert bundle.kind == "starter"
        assert bundle.schema == SUPPORTED_SCHEMA == 1
        assert bundle.embedding_model == "all-MiniLM-L6-v2"
        assert bundle.counts == {"models": 45, "benchmarks": 16}
        assert bundle.full_counts["models"] > bundle.counts["models"]
        assert len(bundle.model_entries()) == 45
        assert len(bundle.benchmark_names) == 16
        assert bundle.manifest["signature"] is None and bundle.manifest["key_id"] is None

    def test_starter_files_are_canonical_text(self):
        """Every data file is byte-for-byte the canonical serialization (section 1)."""
        for name in BUNDLE_DATA_FILES:
            raw = (STARTER_BUNDLE_DIR / name).read_bytes()
            assert raw == canonical_json(json.loads(raw)).encode("utf-8"), name
            assert not raw.endswith(b"\n"), name

    def test_manifest_hashes_are_sha256_of_the_file_bytes(self):
        manifest = _manifest(STARTER_BUNDLE_DIR)
        for name in BUNDLE_DATA_FILES:
            digest = hashlib.sha256((STARTER_BUNDLE_DIR / name).read_bytes()).hexdigest()
            assert manifest["files"][name] == digest

    def test_a_tampered_file_is_refused(self, bundle_copy):
        path = bundle_copy / "models.json"
        path.write_bytes(path.read_bytes().replace(b'"openai/gpt-4o"', b'"openai/gpt-4x"', 1))
        with pytest.raises(BundleIntegrityError, match="models.json does not match"):
            load_bundle(bundle_copy)

    def test_reserialized_json_is_refused_even_when_equal_data(self, bundle_copy):
        """Clients hash the exact text -- they never re-serialize to verify."""
        path = bundle_copy / "benchmarks.json"
        path.write_text(json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=2),
                        encoding="utf-8")
        with pytest.raises(BundleIntegrityError, match="benchmarks.json"):
            load_bundle(bundle_copy)

    def test_a_wrong_manifest_hash_is_refused(self, bundle_copy):
        manifest = _manifest(bundle_copy)
        manifest["files"]["centroids.json"] = "0" * 64
        _write_manifest(bundle_copy, manifest)
        with pytest.raises(BundleIntegrityError, match="centroids.json"):
            load_bundle(bundle_copy)

    def test_a_missing_data_file_is_refused(self, bundle_copy):
        (bundle_copy / "training_queries.json").unlink()
        with pytest.raises(BundleIntegrityError, match="training_queries.json"):
            load_bundle(bundle_copy)

    def test_a_missing_manifest_is_refused(self, bundle_copy):
        (bundle_copy / "manifest.json").unlink()
        with pytest.raises(BundleIntegrityError, match="manifest.json missing"):
            load_bundle(bundle_copy)

    def test_a_newer_schema_is_refused_before_reading_data(self, bundle_copy):
        manifest = _manifest(bundle_copy)
        manifest["schema"] = SUPPORTED_SCHEMA + 1
        _write_manifest(bundle_copy, manifest)
        (bundle_copy / "models.json").unlink()  # never reached
        with pytest.raises(BundleSchemaError, match="schema 2 is newer"):
            load_bundle(bundle_copy)

    @pytest.mark.parametrize("schema", [0, -1, "1", 1.5, None, True])
    def test_an_invalid_schema_is_refused(self, bundle_copy, schema):
        manifest = _manifest(bundle_copy)
        manifest["schema"] = schema
        _write_manifest(bundle_copy, manifest)
        with pytest.raises(BundleError, match="invalid schema"):
            load_bundle(bundle_copy)

    def test_schema_error_is_a_bundle_error(self):
        assert issubclass(BundleSchemaError, BundleError)
        assert issubclass(BundleIntegrityError, BundleError)

    @pytest.mark.parametrize("field,value", [("kind", "premium"), ("version", ""),
                                             ("embedding_model", None)])
    def test_invalid_manifest_fields_are_refused(self, bundle_copy, field, value):
        manifest = _manifest(bundle_copy)
        manifest[field] = value
        _write_manifest(bundle_copy, manifest)
        with pytest.raises(BundleError):
            load_bundle(bundle_copy)

    def test_a_manifest_without_a_file_entry_is_refused(self, bundle_copy):
        manifest = _manifest(bundle_copy)
        del manifest["files"]["normalization_ranges.json"]
        _write_manifest(bundle_copy, manifest)
        with pytest.raises(BundleError, match="normalization_ranges.json"):
            load_bundle(bundle_copy)

    def test_inconsistent_benchmark_sets_are_refused(self, bundle_copy):
        """Re-hashed but inconsistent: a range without a taxonomy entry."""
        path = bundle_copy / "normalization_ranges.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["benchmarks"]["Imaginary-Bench"] = {"lo": 0, "hi": 1, "description": ""}
        text = canonical_json(data)
        path.write_bytes(text.encode("utf-8"))
        manifest = _manifest(bundle_copy)
        manifest["files"]["normalization_ranges.json"] = hashlib.sha256(
            text.encode("utf-8")).hexdigest()
        _write_manifest(bundle_copy, manifest)
        with pytest.raises(BundleError, match="Imaginary-Bench"):
            load_bundle(bundle_copy)

    def test_bundle_from_texts_accepts_the_wire_format(self):
        """GET /v1/catalog/live hands over {manifest, files: {name: text}}."""
        manifest = _manifest(STARTER_BUNDLE_DIR)
        files = {name: (STARTER_BUNDLE_DIR / name).read_text(encoding="utf-8")
                 for name in BUNDLE_DATA_FILES}
        bundle = bundle_from_texts(manifest, files)
        assert bundle.version == starter_bundle().version
        assert bundle.benchmark_names == starter_bundle().benchmark_names
        with pytest.raises(BundleIntegrityError):
            bundle_from_texts(manifest, {**files, "models.json": files["models.json"] + " "})
        with pytest.raises(BundleIntegrityError, match="missing"):
            bundle_from_texts(manifest, {k: v for k, v in files.items()
                                         if k != "benchmarks.json"})


# --------------------------------------------------------------- the seam
class TestResolveBundle:
    def test_default_is_the_packaged_starter(self):
        assert resolve_bundle() is starter_bundle()
        assert resolve_bundle(None) is starter_bundle()

    def test_a_bundle_object_passes_through(self):
        bundle = starter_bundle()
        assert resolve_bundle(bundle) is bundle

    def test_a_path_is_loaded(self, bundle_copy):
        bundle = resolve_bundle(bundle_copy)
        assert bundle.directory == bundle_copy
        assert bundle.version == starter_bundle().version
        assert resolve_bundle(str(bundle_copy)).version == bundle.version

    def test_anything_else_is_a_type_error(self):
        with pytest.raises(TypeError):
            resolve_bundle(42)

    def test_router_takes_all_of_its_data_from_the_bundle(self, bundle_copy):
        router = Router(bundle=bundle_copy)
        assert router.bundle.directory == bundle_copy
        assert len(router.models) == 45
        assert router.benchmarks.names == starter_bundle().benchmark_names
        assert Router().bundle is starter_bundle()

    def test_registries_default_to_the_starter_and_accept_a_bundle(self, bundle_copy):
        assert len(ModelRegistry.default()) == 45
        assert len(ModelRegistry.default(bundle=bundle_copy)) == 45
        assert BenchmarkRegistry.default().names == starter_bundle().benchmark_names

    def test_router_on_the_full_bundle(self, full_bundle):
        router = Router(bundle=full_bundle)
        assert router.bundle.kind == "full"
        assert len(router.models) == 322
        assert len(router.benchmarks) == 33
        normalizer = router.benchmarks.get_normalizer()
        assert normalizer.get_weight("SWE-bench-verified") == (
            full_bundle.benchmark_weights()["SWE-bench-verified"])
        assert normalizer.get_range("MMLU") is not None


# ----------------------------------------------------- starter selection
def _build_script():
    path = REPO_ROOT / "scripts" / "build-catalog-bundles.py"
    if not path.is_file():
        pytest.skip("scripts/ not present (packaged checkout)")
    if not (REPO_ROOT / "shared" / "models" / "default_models.json").is_file():
        pytest.skip("full-catalog build inputs (shared/models, ...) not present")
    spec = importlib.util.spec_from_file_location("_build_catalog_bundles", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestStarterSelection:
    def test_committed_starter_is_reproducible_from_the_build_inputs(self):
        """shared/catalog/starter == a fresh build from the committed inputs."""
        build = _build_script()
        result = build.build(full_out=REPO_ROOT / "build" / "catalog" / "full")
        stale = build.check_starter(result)
        assert stale == [], (
            f"shared/catalog/starter is stale ({stale}) -- run "
            "python scripts/build-catalog-bundles.py"
        )

    def test_selection_follows_the_coverage_rule(self):
        build = _build_script()
        report = build.build()["report"]
        assert report["kept"] == [b for b in report["by_rule"]], (
            "committed starter benchmarks differ from the coverage rule"
        )
        assert 10 <= len(report["kept"]) <= 16
        n = report["n_models"]
        for name in report["kept"]:
            assert report["coverage"][name] / n >= report["threshold"]

    def test_selection_starts_from_the_legacy_built_in_catalog(self):
        selection = json.loads(
            (REPO_ROOT / "shared" / "catalog" / "starter-selection.json").read_text(
                encoding="utf-8"))
        legacy = [m for m in selection["models"] if m["legacy_id"]]
        assert len(legacy) == 39  # every model of the 0.5.x built-in catalog
        ids = [m["id"] for m in selection["models"]]
        assert ids == [m["model_id"] for m in starter_bundle().model_entries()]

    def test_starter_ranges_equal_the_full_ranges(self, full_bundle):
        """A model scores the same on both catalogs for shared benchmarks."""
        full = full_bundle.range_entries()
        for name, entry in starter_bundle().range_entries().items():
            # The starter drops only the full-catalog coverage count (n_real).
            expected = {k: v for k, v in full[name].items() if k != "n_real"}
            assert entry == expected, name

    def test_starter_models_carry_only_starter_benchmarks(self, full_bundle):
        names = set(starter_bundle().benchmark_names)
        full_models = {m["model_id"]: m for m in full_bundle.model_entries()}
        for model in starter_bundle().model_entries():
            assert set(model["benchmark_scores"]) <= names, model["model_id"]
            reference = full_models[model["model_id"]]
            expected = {k: v for k, v in reference["benchmark_scores"].items() if k in names}
            assert model["benchmark_scores"] == expected
            for field in ("pricing", "tokens_per_second", "ttft_ms", "latency", "provider"):
                assert model.get(field) == reference.get(field), (model["model_id"], field)

    def test_starter_centroids_are_the_full_centroids(self, full_bundle):
        full = full_bundle.centroids["centroids"]
        for name, vector in starter_bundle().centroids["centroids"].items():
            assert vector == full[name], name
        full_q = full_bundle.training_query_map()
        for name, queries in starter_bundle().training_query_map().items():
            assert queries == full_q[name], name


# ------------------------------------------------------- engine parity
def _python_scores(bundle, fixture: dict) -> list[dict]:
    registry = ModelRegistry.from_bundle(bundle)
    engine = ScoringEngine(normalizer=BenchmarkRegistry.from_bundle(bundle).get_normalizer())
    coverage = registry.benchmark_coverage()
    rows = []
    for index, item in enumerate(fixture["prompts"]):
        for q, c, s in fixture["priorities"]:
            scores = engine.score_models(
                registry.all_models, item["similarities"], Priorities(q, c, s),
                top_k=10, benchmark_coverage=coverage,
            )
            rows.append({
                "prompt": index,
                "priorities": [q, c, s],
                "scores": [
                    {
                        "model_id": sc.model_id,
                        "final_score": sc.final_score,
                        "quality_score": sc.quality_score,
                        "cost_score": sc.cost_score,
                        "speed_score": sc.speed_score,
                        "q_prime": sc.q_prime,
                        "u_cost": sc.u_cost,
                        "u_speed": sc.u_speed,
                        "in_band": sc.in_band,
                        "reasoning": sc.reasoning,
                    }
                    for sc in scores
                ],
            })
    return rows


@pytest.mark.skipif(
    NODE_EXE is None or not NODE_DIST_INDEX.is_file(),
    reason="needs node on PATH and a built Node SDK (npm run build in packages/node)",
)
def test_python_and_node_score_the_starter_prompt_set_identically():
    fixture_path = PARITY / "starter_prompts.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["prompts"] and fixture["priorities"]
    proc = subprocess.run(
        [NODE_EXE, str(Path(__file__).parent / "node_engine_parity.mjs"),
         str(NODE_DIST_INDEX), str(fixture_path), "starter"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert proc.returncode == 0, proc.stderr
    node = json.loads(proc.stdout)
    assert node["version"] == starter_bundle().version, (
        "the Node dist ships a different starter bundle -- run npm run build"
    )
    python = _python_scores(starter_bundle(), fixture)
    assert len(python) == len(node["rows"]) == len(fixture["prompts"]) * len(
        fixture["priorities"])
    for py_row, node_row in zip(python, node["rows"]):
        label = f"prompt {py_row['prompt']} at {py_row['priorities']}"
        assert [s["model_id"] for s in py_row["scores"]] == [
            s["model_id"] for s in node_row["scores"]], label
        for py_s, node_s in zip(py_row["scores"], node_row["scores"]):
            for key in ("final_score", "quality_score", "cost_score", "speed_score",
                        "q_prime", "u_cost", "u_speed"):
                assert abs(py_s[key] - node_s[key]) <= 1e-9, (label, py_s["model_id"], key)
            assert py_s["in_band"] == node_s["in_band"], (label, py_s["model_id"])
            assert py_s["reasoning"] == node_s["reasoning"], (label, py_s["model_id"])


def test_the_starter_prompt_set_matches_the_starter_benchmarks():
    fixture = json.loads((PARITY / "starter_prompts.json").read_text(encoding="utf-8"))
    names = set(starter_bundle().benchmark_names)
    for item in fixture["prompts"]:
        assert set(item["similarities"]) == names


# ---------------------------------------------- full-catalog regression
def test_full_bundle_reproduces_the_pre_bundle_routing(full_bundle):
    """Routing the recorded similarities on the full bundle gives the same picks
    (and scores) the pre-bundle engine produced on the same 362-model catalog."""
    path = PARITY / "full_baseline_routes.json"
    if not path.is_file():
        pytest.skip("full-catalog routing baseline not present")
    baseline = json.loads(path.read_text(encoding="utf-8"))
    registry = ModelRegistry.from_bundle(full_bundle)
    assert len(registry) == baseline["n_routable_models"]
    engine = ScoringEngine(normalizer=BenchmarkRegistry.from_bundle(full_bundle).get_normalizer())
    coverage = registry.benchmark_coverage()
    mismatches = []
    for route in baseline["routes"]:
        sims = baseline["prompts"][route["prompt"]]["similarities"]
        q, c, s = route["priorities"]
        scores = engine.score_models(registry.all_models, sims, Priorities(q, c, s),
                                     top_k=5, benchmark_coverage=coverage)
        got = [sc.model_id for sc in scores]
        if got != route["top"]:
            mismatches.append((route["prompt"], route["priorities"], route["top"], got))
            continue
        assert [sc.final_score for sc in scores] == route["final"]
        assert [sc.q_prime for sc in scores] == pytest.approx(route["q_prime"], abs=1e-12)
    assert mismatches == [], f"{len(mismatches)} routes changed, e.g. {mismatches[:3]}"


def test_full_bundle_dir_constant_points_at_the_build_output():
    assert FULL_BUNDLE_DIR == REPO_ROOT / "build" / "catalog" / "full"
    if (FULL_BUNDLE_DIR / "manifest.json").is_file():
        assert load_full_bundle().kind == "full"
