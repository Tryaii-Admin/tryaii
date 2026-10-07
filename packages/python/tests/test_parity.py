"""Cross-SDK parity guards.

The Node and Python SDKs are meant to make identical routing/scoring decisions.
They have silently drifted before (mismatched ARC scores, pricing, MT-Bench
ranges), so these tests fail loudly the moment the shipped data diverges again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tryaii.benchmarks.standard import STANDARD_BENCHMARKS
from tryaii.scoring.benchmarks import NORMALIZATION_RANGES

REPO_ROOT = Path(__file__).resolve().parents[3]
# The packages' built-in routing data is the starter catalog bundle
# (docs/catalog/CONTRACT-catalog-v1.md); shared/catalog/starter is its master.
SHARED_STARTER = REPO_ROOT / "shared" / "catalog" / "starter"
PY_STARTER = REPO_ROOT / "packages" / "python" / "tryaii" / "catalog" / "data" / "starter"
NODE_STARTER = REPO_ROOT / "packages" / "node" / "src" / "catalog" / "data" / "starter"
STARTER_FILES = (
    "manifest.json",
    "models.json",
    "benchmarks.json",
    "normalization_ranges.json",
    "centroids.json",
    "training_queries.json",
)
NODE_PRESET = NODE_STARTER / "models.json"
PY_PRESET = PY_STARTER / "models.json"
NODE_CLI = REPO_ROOT / "packages" / "node" / "src" / "cli.ts"
SHARED_RANGES = SHARED_STARTER / "normalization_ranges.json"
PY_RANGES = PY_STARTER / "normalization_ranges.json"
NODE_RANGES = NODE_STARTER / "normalization_ranges.json"


def _by_model_id(raw: dict | list) -> dict[str, dict]:
    """Index a preset file by model_id, ignoring list ordering."""
    models = raw["models"] if isinstance(raw, dict) and "models" in raw else raw
    return {m["model_id"]: m for m in models}


def test_preset_model_data_identical_across_sdks():
    """The Node and Python starter catalogs' models must be equal data.

    Pricing feeds cost scoring + the budget knapsack and benchmark scores feed
    quality scoring, so any divergence routes the same prompt to different models
    depending on the SDK language.
    """
    if not NODE_PRESET.exists():
        pytest.skip("Node preset not present (python-only checkout)")

    node = _by_model_id(json.loads(NODE_PRESET.read_text(encoding="utf-8")))
    py = _by_model_id(json.loads(PY_PRESET.read_text(encoding="utf-8")))

    assert set(node) == set(py), (
        f"Model id sets differ: only in node={sorted(set(node) - set(py))}, "
        f"only in python={sorted(set(py) - set(node))}"
    )

    diffs: list[str] = []
    for model_id in sorted(node):
        n, p = node[model_id], py[model_id]
        if n == p:
            continue
        if n.get("pricing") != p.get("pricing"):
            diffs.append(
                f"{model_id}.pricing: node={n.get('pricing')} py={p.get('pricing')}"
            )
        nb, pb = n.get("benchmark_scores", {}), p.get("benchmark_scores", {})
        for k in sorted(set(nb) | set(pb)):
            if nb.get(k) != pb.get(k):
                diffs.append(
                    f"{model_id}.benchmark_scores.{k}: "
                    f"node={nb.get(k)} py={pb.get(k)}"
                )
        if n.get("latency") != p.get("latency"):
            diffs.append(
                f"{model_id}.latency: node={n.get('latency')} py={p.get('latency')}"
            )
        # tokens_per_second and ttft_ms are the two inputs to the continuous
        # speed score; this test used to compare pricing/benchmarks/latency
        # only, so it would not have caught a drift in either of them.
        for field in ("tokens_per_second", "ttft_ms"):
            if n.get(field) != p.get(field):
                diffs.append(
                    f"{model_id}.{field}: node={n.get(field)} py={p.get(field)}"
                )

    assert not diffs, "Node/Python preset data diverged:\n" + "\n".join(diffs)


def test_standalone_ranges_match_standard_benchmarks():
    """The standalone NORMALIZATION_RANGES must agree with the routing path.

    STANDARD_BENCHMARKS is what the default router actually uses; the standalone
    BenchmarkNormalizer table must not disagree with it (the MT-Bench 5-vs-6 bug).
    """
    diffs: list[str] = []
    for bench in STANDARD_BENCHMARKS:
        standalone = NORMALIZATION_RANGES.get(bench.name)
        if standalone is None:
            continue
        if (standalone.min_score, standalone.max_score) != (
            bench.normalization.min_score,
            bench.normalization.max_score,
        ):
            diffs.append(
                f"{bench.name}: standalone=({standalone.min_score}, {standalone.max_score}) "
                f"standard=({bench.normalization.min_score}, {bench.normalization.max_score})"
            )
    assert not diffs, "NORMALIZATION_RANGES disagree with STANDARD_BENCHMARKS:\n" + "\n".join(diffs)


def test_normalization_ranges_identical_across_sdks():
    """Both SDKs must ship byte-identical copies of the starter range table.

    The ranges used to be hand-duplicated in benchmarks.py and benchmarks.ts,
    guarded only by a Python-to-Python test -- a silent-drift surface with no
    cross-language guard at all. They are now generated once, built into
    shared/catalog/starter/normalization_ranges.json and copied verbatim by
    scripts/sync-shared.py, so an un-synced edit is a byte difference here.
    """
    assert SHARED_RANGES.exists(), "shared/catalog/starter/normalization_ranges.json missing"
    master = SHARED_RANGES.read_bytes()

    for label, path in (("python", PY_RANGES), ("node", NODE_RANGES)):
        if not path.exists():
            pytest.skip(f"{label} range copy not present (partial checkout)")
        assert path.read_bytes() == master, (
            f"{label} copy of normalization_ranges.json differs from shared/ -- "
            "run python scripts/sync-shared.py"
        )


def test_normalization_ranges_match_packaged_json():
    """NORMALIZATION_RANGES must be exactly what the packaged JSON says.

    Guards the import-time load: a stale in-memory table (or a renamed field)
    would silently score every model against the wrong scale.
    """
    data = json.loads(PY_RANGES.read_text(encoding="utf-8"))
    entries = data["benchmarks"]

    assert set(NORMALIZATION_RANGES) == set(entries), (
        "NORMALIZATION_RANGES keys differ from the JSON: "
        f"only in table={sorted(set(NORMALIZATION_RANGES) - set(entries))}, "
        f"only in json={sorted(set(entries) - set(NORMALIZATION_RANGES))}"
    )

    diffs: list[str] = []
    for name, entry in sorted(entries.items()):
        rng = NORMALIZATION_RANGES[name]
        actual = (rng.min_score, rng.max_score, rng.description)
        expected = (entry["lo"], entry["hi"], entry["description"])
        if actual != expected:
            diffs.append(f"{name}: table={actual} json={expected}")
    assert not diffs, "NORMALIZATION_RANGES disagree with the packaged JSON:\n" + "\n".join(diffs)


def _node_template_literal(source: str, const_name: str) -> str:
    """Extract the raw text of a `const <NAME> = `...`;` template literal.

    The help strings are deliberately backtick-free and ${-free, so the raw
    source between the backticks equals the runtime string -- which lets us
    compare it directly to the Python value without evaluating JS.
    """
    marker = f"const {const_name} = `"
    start = source.index(marker) + len(marker)
    return source[start : source.index("`;", start)]


def test_cli_help_text_identical_across_sdks():
    """Both CLIs must print byte-identical --help text.

    The help text documents the full CLI surface (commands, flags, defaults),
    so a drift here means users get a different experience per SDK -- exactly
    the gap that previously shipped --version as Node-only and --verbose as
    Python-only.
    """
    if not NODE_CLI.exists():
        pytest.skip("Node CLI source not present (python-only checkout)")

    from tryaii.cli.main import HELP

    source = NODE_CLI.read_text(encoding="utf-8")
    node_help = _node_template_literal(source, "HELP")

    assert node_help == HELP, (
        "CLI help text diverged between packages/node/src/cli.ts and "
        "packages/python/tryaii/cli/main.py -- keep both HELP blocks identical"
    )


# command name -> the constant that holds its help text in both CLIs.
COMMAND_HELP_CONSTANTS = {
    "route": "HELP_ROUTE",
    "eval": "HELP_EVAL",
    "cachelint": "HELP_CACHELINT",
    "diagnose": "HELP_DIAGNOSE",
    "designpartner": "HELP_DESIGNPARTNER",
    "models": "HELP_MODELS",
    "benchmarks": "HELP_BENCHMARKS",
    "setup": "HELP_SETUP",
    "regenerate": "HELP_REGENERATE",
    "login": "HELP_LOGIN",
    "logout": "HELP_LOGOUT",
    "whoami": "HELP_WHOAMI",
    "help": "HELP_HELP",
}

# diagnose verb -> the constant that holds its verb help in both CLIs
# (`tryaii diagnose <verb> --help`).
DIAGNOSE_VERB_HELP_CONSTANTS = {
    "init": "HELP_DIAGNOSE_INIT",
    "plan": "HELP_DIAGNOSE_PLAN",
    "check": "HELP_DIAGNOSE_CHECK",
    "report": "HELP_DIAGNOSE_REPORT",
}


def test_command_help_text_identical_across_sdks():
    """Per-command help (`tryaii help <cmd>` / `tryaii <cmd> --help`) must match.

    Same rationale as the global help: a drift means npm and pip users see a
    different help page for the same command.
    """
    if not NODE_CLI.exists():
        pytest.skip("Node CLI source not present (python-only checkout)")

    from tryaii.cli.main import COMMAND_HELP

    assert set(COMMAND_HELP) == set(COMMAND_HELP_CONSTANTS), (
        "Python COMMAND_HELP keys differ from the expected command set: "
        f"python={sorted(COMMAND_HELP)} expected={sorted(COMMAND_HELP_CONSTANTS)}"
    )

    source = NODE_CLI.read_text(encoding="utf-8")
    diffs: list[str] = []
    for command, const_name in COMMAND_HELP_CONSTANTS.items():
        node_text = _node_template_literal(source, const_name)
        if node_text != COMMAND_HELP[command]:
            diffs.append(command)

    assert not diffs, (
        "Per-command help diverged between Node and Python for: "
        + ", ".join(diffs)
        + " -- keep the HELP_<CMD> blocks identical across both CLIs"
    )


def test_diagnose_verb_help_text_identical_across_sdks():
    """Diagnose verb help (`tryaii diagnose <verb> --help`) must match too."""
    if not NODE_CLI.exists():
        pytest.skip("Node CLI source not present (python-only checkout)")

    from tryaii.cli.main import DIAGNOSE_VERB_HELP

    assert set(DIAGNOSE_VERB_HELP) == set(DIAGNOSE_VERB_HELP_CONSTANTS), (
        "Python DIAGNOSE_VERB_HELP keys differ from the expected verb set: "
        f"python={sorted(DIAGNOSE_VERB_HELP)} "
        f"expected={sorted(DIAGNOSE_VERB_HELP_CONSTANTS)}"
    )

    source = NODE_CLI.read_text(encoding="utf-8")
    diffs: list[str] = []
    for verb, const_name in DIAGNOSE_VERB_HELP_CONSTANTS.items():
        node_text = _node_template_literal(source, const_name)
        if node_text != DIAGNOSE_VERB_HELP[verb]:
            diffs.append(verb)

    assert not diffs, (
        "Diagnose verb help diverged between Node and Python for: "
        + ", ".join(diffs)
        + " -- keep the HELP_DIAGNOSE_<VERB> blocks identical across both CLIs"
    )


# ---------------------------------------------------------------------------
# cachelint provider knowledge base
# ---------------------------------------------------------------------------

SHARED_CACHELINT_KB = REPO_ROOT / "shared" / "cachelint" / "providers.json"
PY_CACHELINT_KB = (
    REPO_ROOT / "packages" / "python" / "tryaii" / "cachelint" / "data" / "providers.json"
)
NODE_CACHELINT_KB = (
    REPO_ROOT / "packages" / "node" / "src" / "cachelint" / "data" / "providers.json"
)


def test_cachelint_providers_identical_across_sdks():
    """Both SDKs must ship byte-identical cachelint knowledge bases.

    Thresholds feed verdicts (BELOW_THRESHOLD vs CACHEABLE is a token
    comparison against this data), so any drift routes the same prompt to a
    different verdict depending on the SDK language.
    """
    if not NODE_CACHELINT_KB.exists():
        pytest.skip("Node cachelint data not present (python-only checkout)")

    assert PY_CACHELINT_KB.read_bytes() == NODE_CACHELINT_KB.read_bytes(), (
        "cachelint providers.json diverged between the two SDKs -- edit "
        "shared/cachelint/providers.json and run scripts/sync-shared.py"
    )


def test_cachelint_package_data_matches_shared_master():
    """Each package's bundled KB must equal the shared/ master it is synced from."""
    if not SHARED_CACHELINT_KB.exists():
        pytest.skip("shared/cachelint not present (package-only checkout)")

    master = SHARED_CACHELINT_KB.read_bytes()
    stale = [
        str(path.relative_to(REPO_ROOT))
        for path in (PY_CACHELINT_KB, NODE_CACHELINT_KB)
        if path.exists() and path.read_bytes() != master
    ]
    assert not stale, (
        "Package copies out of sync with shared/cachelint/providers.json: "
        + ", ".join(stale)
        + " -- run scripts/sync-shared.py"
    )


# ---------------------------------------------------------------------------
# diagnose shared data (plan.json + costmodel.json)
# ---------------------------------------------------------------------------

SHARED_DIAGNOSE = REPO_ROOT / "shared" / "diagnose"
PY_DIAGNOSE_DATA = REPO_ROOT / "packages" / "python" / "tryaii" / "diagnose" / "data"
NODE_DIAGNOSE_DATA = REPO_ROOT / "packages" / "node" / "src" / "diagnose" / "data"

DIAGNOSE_DATA_FILES = ("plan.json", "costmodel.json")


@pytest.mark.parametrize("filename", DIAGNOSE_DATA_FILES)
def test_diagnose_data_identical_across_sdks(filename):
    """Both SDKs must ship byte-identical diagnose data files.

    plan.json IS the `diagnose plan --json` output; costmodel.json feeds the
    cache-savings estimate — any drift makes the same inventory produce
    different findings depending on the SDK language.
    """
    node_copy = NODE_DIAGNOSE_DATA / filename
    if not node_copy.exists():
        pytest.skip("Node diagnose data not present (python-only checkout)")

    assert (PY_DIAGNOSE_DATA / filename).read_bytes() == node_copy.read_bytes(), (
        f"diagnose {filename} diverged between the two SDKs -- edit "
        f"shared/diagnose/{filename} and run scripts/sync-shared.py"
    )


@pytest.mark.parametrize("filename", DIAGNOSE_DATA_FILES)
def test_diagnose_package_data_matches_shared_master(filename):
    """Each package's bundled diagnose data must equal the shared/ master."""
    master_path = SHARED_DIAGNOSE / filename
    if not master_path.exists():
        pytest.skip("shared/diagnose not present (package-only checkout)")

    master = master_path.read_bytes()
    stale = [
        str(path.relative_to(REPO_ROOT))
        for path in (PY_DIAGNOSE_DATA / filename, NODE_DIAGNOSE_DATA / filename)
        if path.exists() and path.read_bytes() != master
    ]
    assert not stale, (
        f"Package copies out of sync with shared/diagnose/{filename}: "
        + ", ".join(stale)
        + " -- run scripts/sync-shared.py"
    )


# Packed masters: output filename -> [(payload key, master path), ...].
# Must mirror PACKS in scripts/sync-shared.py.
DIAGNOSE_PACKS = {
    "report_template.json": [
        ("html", SHARED_DIAGNOSE / "report" / "template.html"),
    ],
    "skill.json": [
        ("skill_md", SHARED_DIAGNOSE / "skill" / "SKILL.md"),
        ("agents_pointer_md", SHARED_DIAGNOSE / "skill" / "agents-pointer.md"),
    ],
}

# ---------------------------------------------------------------------------
# designpartner shared data (questions.json + packed skill)
# ---------------------------------------------------------------------------

SHARED_DESIGNPARTNER = REPO_ROOT / "shared" / "designpartner"
PY_DP_DATA = (
    REPO_ROOT / "packages" / "python" / "tryaii" / "designpartner" / "data")
NODE_DP_DATA = REPO_ROOT / "packages" / "node" / "src" / "designpartner" / "data"


def test_designpartner_questions_identical_and_match_master():
    """Both SDKs must ship byte-identical questionnaire catalogs equal to the
    shared master — the catalog IS the questionnaire + the consent copy, so
    drift means users see different questions or different consent terms
    depending on the SDK language."""
    master_path = SHARED_DESIGNPARTNER / "questions.json"
    node_copy = NODE_DP_DATA / "questions.json"
    if not master_path.exists() or not node_copy.exists():
        pytest.skip("designpartner shared data not present (partial checkout)")

    master = master_path.read_bytes()
    stale = [
        str(path.relative_to(REPO_ROOT))
        for path in (PY_DP_DATA / "questions.json", node_copy)
        if path.read_bytes() != master
    ]
    assert not stale, (
        "Package copies out of sync with shared/designpartner/questions.json: "
        + ", ".join(stale) + " -- run scripts/sync-shared.py"
    )


def test_designpartner_packed_skill_matches_shared_masters():
    """The packed designpartner skill must equal a re-pack of its masters."""
    parts = [
        ("skill_md", SHARED_DESIGNPARTNER / "skill" / "SKILL.md"),
        ("agents_pointer_md",
         SHARED_DESIGNPARTNER / "skill" / "agents-pointer.md"),
    ]
    if not all(master.exists() for _key, master in parts):
        pytest.skip("shared/designpartner not present (package-only checkout)")

    expected = json.dumps(
        {key: master.read_text(encoding="utf-8") for key, master in parts},
        ensure_ascii=False, indent=2) + "\n"
    stale = [
        str(path.relative_to(REPO_ROOT))
        for path in (PY_DP_DATA / "skill.json", NODE_DP_DATA / "skill.json")
        if path.exists()
        and path.read_text(encoding="utf-8") != expected
    ]
    assert not stale, (
        "Packed designpartner skill.json out of sync with its shared "
        "masters: " + ", ".join(stale) + " -- run scripts/sync-shared.py"
    )


@pytest.mark.parametrize("filename", sorted(DIAGNOSE_PACKS))
def test_diagnose_packed_data_matches_shared_masters(filename):
    """Packed diagnose data must equal a re-pack of its shared masters.

    Non-JSON masters (HTML template, skill markdown) ship packed into JSON
    so they flow through the JSON-only asset pipeline; this re-packs them
    with the same serialization sync-shared.py uses and byte-compares both
    package copies.
    """
    parts = DIAGNOSE_PACKS[filename]
    if not all(master.exists() for _key, master in parts):
        pytest.skip("shared/diagnose not present (package-only checkout)")

    expected = json.dumps(
        {key: master.read_text(encoding="utf-8") for key, master in parts},
        ensure_ascii=False, indent=2) + "\n"
    stale = [
        str(path.relative_to(REPO_ROOT))
        for path in (PY_DIAGNOSE_DATA / filename, NODE_DIAGNOSE_DATA / filename)
        if path.exists()
        and path.read_text(encoding="utf-8") != expected
    ]
    assert not stale, (
        f"Packed {filename} out of sync with its shared/diagnose masters: "
        + ", ".join(stale)
        + " -- run scripts/sync-shared.py"
    )


# --- Catalog bundle data parity --------------------------------------------
#
# The starter catalog bundle (six files) is copied into both SDKs by
# scripts/sync-shared.py. Routing decisions are only cross-SDK identical if all
# copies are byte-identical; the manifest pins each data file's sha256, so the
# loaders also refuse a copy that was edited in place.

NODE_TRAINING = NODE_STARTER / "training_queries.json"
PY_TRAINING = PY_STARTER / "training_queries.json"
NODE_CENTROIDS = NODE_STARTER / "centroids.json"
PY_CENTROIDS = PY_STARTER / "centroids.json"
SHARED = REPO_ROOT / "shared"


def test_training_queries_identical_across_sdks():
    """Different training queries would build different centroids per SDK."""
    if not NODE_TRAINING.exists():
        pytest.skip("Node training queries not present (python-only checkout)")
    assert NODE_TRAINING.read_bytes() == PY_TRAINING.read_bytes()


def test_centroids_identical_across_sdks():
    """Different centroids classify the same prompt differently per SDK."""
    if not NODE_CENTROIDS.exists():
        pytest.skip("Node centroids not present (python-only checkout)")
    assert NODE_CENTROIDS.read_bytes() == PY_CENTROIDS.read_bytes()


@pytest.mark.parametrize("filename", STARTER_FILES)
def test_starter_bundle_identical_across_sdks(filename):
    node_copy = NODE_STARTER / filename
    if not node_copy.exists():
        pytest.skip("Node starter bundle not present (python-only checkout)")
    assert node_copy.read_bytes() == (PY_STARTER / filename).read_bytes(), (
        f"{filename} differs between the SDKs -- run python scripts/sync-shared.py"
    )


def test_trusted_catalog_keys_identical_across_sdks():
    """Catalog contract section 6: both SDKs must trust exactly the same
    catalog-signing keys (shared/catalog/trusted_keys.json, copied by
    scripts/sync-shared.py)."""
    master = SHARED / "catalog" / "trusted_keys.json"
    py_copy = PY_STARTER.parent / "trusted_keys.json"
    node_copy = NODE_STARTER.parent / "trusted_keys.json"
    assert py_copy.read_bytes() == master.read_bytes(), "run python scripts/sync-shared.py"
    if not node_copy.exists():
        pytest.skip("Node package data not present (python-only checkout)")
    assert node_copy.read_bytes() == master.read_bytes(), "run python scripts/sync-shared.py"


def test_package_data_matches_shared_master():
    """shared/ is the master copy; the bundled package files must match it.

    A drift here means someone edited a package copy directly instead of
    editing shared/ and running scripts/sync-shared.py.
    """
    shared_map = {SHARED_STARTER / name: PY_STARTER / name for name in STARTER_FILES}
    if not SHARED.exists():
        pytest.skip("shared/ not present (packaged checkout)")
    for master, copy in shared_map.items():
        assert master.read_bytes() == copy.read_bytes(), f"{copy} drifted from {master}"


# The FULL catalog must never be package data again: these are the paths the
# pre-bundle releases shipped it under, in both SDKs.
_RETIRED_FULL_DATA = (
    "packages/python/tryaii/registry/presets/default_models.json",
    "packages/python/tryaii/scoring/data/normalization_ranges.json",
    "packages/python/tryaii/centroids/data/centroids_all-MiniLM-L6-v2.json",
    "packages/python/tryaii/centroids/data/training_queries.json",
    "packages/node/src/registry/presets/defaultModels.json",
    "packages/node/src/scoring/data/normalization_ranges.json",
    "packages/node/src/centroids/data/centroids_all-MiniLM-L6-v2.json",
    "packages/node/src/centroids/data/trainingQueries.json",
)


@pytest.mark.parametrize("relative", _RETIRED_FULL_DATA)
def test_full_catalog_is_not_package_data(relative):
    assert not (REPO_ROOT / relative).exists(), (
        f"{relative} is back -- the full catalog must not ship in the package "
        "(it is downloaded after login); package data is the starter bundle only"
    )


def test_packaged_starter_is_a_starter_bundle():
    """The bundle shipped in the package is the small starter catalog."""
    manifest = json.loads((PY_STARTER / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "starter"
    assert manifest["counts"]["models"] <= 60
    assert manifest["counts"]["benchmarks"] <= 16
    assert manifest["full_counts"]["models"] > manifest["counts"]["models"]
