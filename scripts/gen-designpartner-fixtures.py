#!/usr/bin/env python
"""Regenerate the machine-generated parts of shared/designpartner/fixtures/.

The Python engine (packages/python/tryaii/designpartner) is the REFERENCE
implementation. This script runs each suite's target over the hand-written
inputs and rewrites, in place, every case's `expected` block in
<suite>/cases.json (plus cli stdout goldens).

Inputs, case names, and descriptions are never touched. Regenerated output
must be hand-reviewed before commit — the frozen files are the conformance
contract for the TypeScript port (see shared/designpartner/SPEC.md §7).

Usage:
  python scripts/gen-designpartner-fixtures.py               # all suites
  python scripts/gen-designpartner-fixtures.py --suite cli   # one suite
  python scripts/gen-designpartner-fixtures.py --check       # exit 1 on drift
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
FIXTURES = REPO / "shared" / "designpartner" / "fixtures"
PKG_PYTHON = REPO / "packages" / "python"

sys.path.insert(0, str(PKG_PYTHON))

SUITES = ("catalog", "validate", "payload", "state", "cli")


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _load(inp: dict, key: str):
    """Inline value or a corpus file reference `<key>_file`."""
    if key in inp:
        return inp[key]
    path = FIXTURES / inp[f"{key}_file"]
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Per-suite generators
# ---------------------------------------------------------------------------

def gen_catalog(inp):
    from tryaii.designpartner.catalog import (
        all_questions,
        applicable_questions,
        load_catalog,
    )

    catalog = load_catalog()
    answers = _load(inp, "answers")
    applicable = applicable_questions(catalog, answers)
    applicable_set = set(applicable)
    return {
        "applicable": applicable,
        "required": [q["id"] for q in all_questions(catalog)
                     if q["required"] and q["id"] in applicable_set],
    }


def gen_validate(inp):
    from tryaii.cachelint._jsonutil import normalize_numbers
    from tryaii.designpartner.catalog import load_catalog, validate_answers

    return normalize_numbers(validate_answers(load_catalog(), _load(inp, "answers")))


def gen_payload(inp):
    from tryaii.designpartner.payload import build_submission

    state = {
        "answers": _load(inp, "answers"),
        "consent": inp["consent"],
    }
    diagnose_docs = None
    if "diagnose_docs" in inp or "diagnose_docs_file" in inp:
        diagnose_docs = _load(inp, "diagnose_docs")
    return build_submission(
        state, diagnose_docs,
        version=inp.get("version", "0.0.0"),
        submitted_at=inp.get("submitted_at"),
        confirmed_at=inp.get("confirmed_at"),
    )


def gen_state(inp):
    from tryaii.designpartner.catalog import load_catalog, tiers_by_id
    from tryaii.designpartner.state import derive_stage

    stage = derive_stage(inp["state"], inp["has_diagnose_run"],
                         tiers_by_id(load_catalog()))
    return {"stage": stage}


def run_cli_case(inp: dict) -> subprocess.CompletedProcess:
    """Run the Python CLI for a cli-suite case in a FRESH temp cwd.

    Same driver contract as the diagnose cli suite (copy_from_corpus,
    pre_argv, stdin_file) PLUS `env`: an object merged into the subprocess
    environment — how the unroutable TRYAII_DESIGNPARTNER_URL reaches the
    CLI. Written-file parity is asserted by test_designpartner_cli_parity.
    """
    env = dict(os.environ,
               TRYAII_NO_BANNER="1",
               PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(PKG_PYTHON))
    env.update(inp.get("env", {}))
    stdin_data = None
    if "stdin_file" in inp:
        stdin_data = (FIXTURES / inp["stdin_file"]).read_bytes()
    with tempfile.TemporaryDirectory() as tmp:
        for name in inp.get("copy_from_corpus", []):
            shutil.copy2(FIXTURES / "corpus" / name, Path(tmp) / name)
        for pre in inp.get("pre_argv", []):
            subprocess.run(
                [sys.executable, "-c",
                 "from tryaii.cli.main import cli; cli()", *pre],
                capture_output=True, cwd=tmp, env=env, check=True,
            )
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
        if not (FIXTURES / name / "cases.json").exists():
            continue
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
