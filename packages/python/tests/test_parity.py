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
NODE_PRESET = (
    REPO_ROOT
    / "packages"
    / "node"
    / "src"
    / "registry"
    / "presets"
    / "defaultModels.json"
)
PY_PRESET = (
    REPO_ROOT
    / "packages"
    / "python"
    / "tryaii"
    / "registry"
    / "presets"
    / "default_models.json"
)
NODE_CLI = REPO_ROOT / "packages" / "node" / "src" / "cli.ts"


def _by_model_id(raw: dict | list) -> dict[str, dict]:
    """Index a preset file by model_id, ignoring list ordering."""
    models = raw["models"] if isinstance(raw, dict) and "models" in raw else raw
    return {m["model_id"]: m for m in models}


def test_preset_model_data_identical_across_sdks():
    """The Node and Python default model presets must be byte-for-byte equal data.

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
    "models": "HELP_MODELS",
    "benchmarks": "HELP_BENCHMARKS",
    "setup": "HELP_SETUP",
    "regenerate": "HELP_REGENERATE",
    "help": "HELP_HELP",
}

# diagnose verb -> the constant that holds its verb help in both CLIs
# (`tryaii diagnose <verb> --help`).
DIAGNOSE_VERB_HELP_CONSTANTS = {
    "plan": "HELP_DIAGNOSE_PLAN",
    "check": "HELP_DIAGNOSE_CHECK",
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
