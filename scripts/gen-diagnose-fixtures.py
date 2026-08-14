#!/usr/bin/env python
"""Regenerate the machine-generated parts of shared/diagnose/fixtures/.

The Python engine (packages/python/tryaii/diagnose) is the REFERENCE
implementation. This script runs each suite's target over the hand-written
inputs and rewrites, in place, every case's `expected` block in
<suite>/cases.json.

Inputs, case names, and descriptions are never touched. Regenerated output
must be hand-reviewed before commit — the frozen files are the conformance
contract for the TypeScript port (see shared/diagnose/SPEC.md §5).

All suites are routing-free: model_fit cases always use the site
`_classification` seam, so no fixture ever needs the embedding model.
Catalog syncs (shared/models/default_models.json) are expected to churn the
modelfit/cost/check suites — regenerate and review alongside the sync.

Usage:
  python scripts/gen-diagnose-fixtures.py               # all suites
  python scripts/gen-diagnose-fixtures.py --suite check # one suite
  python scripts/gen-diagnose-fixtures.py --check       # no writes; exit 1 on drift
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "shared" / "diagnose" / "fixtures"
PKG_PYTHON = REPO / "packages" / "python"

sys.path.insert(0, str(PKG_PYTHON))

SUITES = ("intake", "resolve", "modelfit", "cost", "hygiene", "check", "cli")


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _load_input_data(inp: dict):
    """Resolve an inventory-shaped input: inline `data` or `input_file`."""
    if "data" in inp:
        return inp["data"]
    path = FIXTURES / inp["input_file"]
    return json.loads(path.read_text(encoding="utf-8"))


def _registry():
    from tryaii.registry.models import ModelRegistry

    return ModelRegistry.default()


def _fit_internal(inp):
    """Optional model_fit internals for the cost suite's swap rule."""
    if "classification" not in inp:
        return None
    from tryaii.diagnose.modelfit import run_model_fit
    from tryaii.scoring.priorities import Priorities

    _payload, internal = run_model_fit(
        inp["classification"], inp.get("resolved_model_id"),
        inp.get("declared_model"), Priorities.from_dict(inp.get("priorities") or {}),
        _registry())
    return internal


# ---------------------------------------------------------------------------
# Per-suite generators: input dict -> expected
# ---------------------------------------------------------------------------

def gen_intake(inp):
    from tryaii.cachelint._jsonutil import normalize_numbers
    from tryaii.diagnose.intake import normalize_inventory

    try:
        return normalize_numbers(normalize_inventory(_load_input_data(inp)))
    except ValueError as exc:
        return {"error": str(exc)}


def gen_resolve(inp):
    from tryaii.diagnose.resolve import resolve_model_id

    model_id, method = resolve_model_id(inp.get("model"), _registry())
    return {"model_id": model_id, "method": method}


def gen_modelfit(inp):
    from tryaii.cachelint._jsonutil import normalize_numbers
    from tryaii.diagnose.modelfit import run_model_fit
    from tryaii.scoring.priorities import Priorities

    payload, _internal = run_model_fit(
        inp["classification"], inp.get("resolved_model_id"),
        inp.get("declared_model"), Priorities.from_dict(inp.get("priorities") or {}),
        _registry())
    return normalize_numbers(payload)


def gen_cost(inp):
    from tryaii.cachelint._jsonutil import normalize_numbers
    from tryaii.diagnose.api import _read_discount_factors
    from tryaii.diagnose.cost import run_cost

    return normalize_numbers(run_cost(
        canonical_text=inp.get("canonical_text"),
        resolved_model_id=inp.get("resolved_model_id"),
        declared_model=inp.get("declared_model"),
        registry=_registry(),
        output_tokens=inp.get("output_tokens", 500),
        calls_per_day=inp.get("calls_per_day"),
        cache_ctx=inp.get("cache_ctx"),
        fit_internal=_fit_internal(inp),
        read_discount_factors=_read_discount_factors(),
    ))


def gen_hygiene(inp):
    from tryaii.cachelint._jsonutil import normalize_numbers
    from tryaii.diagnose.hygiene import run_hygiene

    return normalize_numbers(run_hygiene(inp["prompt"]))


def gen_check(inp):
    from tryaii.diagnose.api import analyze_inventory

    try:
        return analyze_inventory(_load_input_data(inp), inp.get("opts") or {})
    except ValueError as exc:
        return {"error": str(exc)}


def run_cli_case(inp: dict) -> subprocess.CompletedProcess:
    """Run the Python CLI for a cli-suite case in a FRESH temp cwd.

    `check` writes a run dir, so cli cases never run inside the fixtures
    tree; `copy_from_corpus` names corpus files staged into the temp cwd.
    Cross-CLI byte parity of the WRITTEN files is asserted separately by
    test_diagnose_cli_parity.py — the frozen goldens cover stdout/stderr.
    """
    env = dict(os.environ,
               TRYAII_NO_BANNER="1",
               PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(PKG_PYTHON))
    stdin_data = None
    if "stdin_file" in inp:
        stdin_data = (FIXTURES / inp["stdin_file"]).read_bytes()
    with tempfile.TemporaryDirectory() as tmp:
        for name in inp.get("copy_from_corpus", []):
            shutil.copy2(FIXTURES / "corpus" / name, Path(tmp) / name)
        return subprocess.run(
            [sys.executable, "-c",
             "from tryaii.cli.main import cli; cli()", *inp["argv"]],
            input=stdin_data, capture_output=True, cwd=tmp, env=env,
        )


def gen_cli(inp, case_name: str):
    proc = run_cli_case(inp)
    stdout = proc.stdout.decode("utf-8").replace("\r\n", "\n")
    stderr = proc.stderr.decode("utf-8").replace("\r\n", "\n")
    golden = case_name + ".stdout.golden.txt"
    return {"stdout_golden": golden,
            "stderr": stderr or None,
            "exit_code": proc.returncode}, {golden: stdout}


# ---------------------------------------------------------------------------

def run_suite(name: str) -> dict:
    """Return {relative_path: new_content_str} for every file this suite owns."""
    suite_dir = FIXTURES / name
    doc = json.loads((suite_dir / "cases.json").read_text(encoding="utf-8"))
    out_files: dict[str, str] = {}

    for case in doc["cases"]:
        if name == "cli":
            expected, goldens = gen_cli(case["input"], case["name"])
            case["expected"] = expected
            for fname, text in goldens.items():
                out_files[f"{name}/{fname}"] = text
        else:
            case["expected"] = globals()["gen_" + name](case["input"])

    out_files[f"{name}/cases.json"] = _dump(doc)
    return out_files


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", choices=SUITES,
                        help="regenerate a single suite")
    parser.add_argument("--check", action="store_true",
                        help="compare instead of writing; exit 1 on drift")
    args = parser.parse_args(argv)

    suites = (args.suite,) if args.suite else SUITES
    drift: list[str] = []

    for name in suites:
        for rel, content in run_suite(name).items():
            path = FIXTURES / rel
            current = (path.read_text(encoding="utf-8")
                       if path.exists() else None)
            if args.check:
                if current != content:
                    drift.append(rel)
            elif current != content:
                path.write_text(content, encoding="utf-8", newline="\n")
                print(f"wrote {rel}")

    if args.check:
        if drift:
            print("DRIFT in:", *drift, sep="\n  ")
            return 1
        print("fixtures up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
