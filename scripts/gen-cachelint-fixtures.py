#!/usr/bin/env python
"""Regenerate the machine-generated parts of shared/cachelint/fixtures/.

The Python engine (packages/python/tryaii/cachelint) is the REFERENCE
implementation. This script runs each suite's target over the hand-written
inputs and rewrites, in place:

  * every case's `expected` block in <suite>/cases.json
  * the report/*.golden.txt and cli/*.stdout.golden.txt files

Inputs, case names, and descriptions are never touched. Regenerated output
must be hand-reviewed before commit — the frozen files are the conformance
contract for the TypeScript port (see shared/cachelint/SPEC.md §5).

Usage:
  python scripts/gen-cachelint-fixtures.py               # all suites
  python scripts/gen-cachelint-fixtures.py --suite cli   # one suite
  python scripts/gen-cachelint-fixtures.py --check       # no writes; exit 1 on drift
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "shared" / "cachelint" / "fixtures"
PKG_PYTHON = REPO / "packages" / "python"

sys.path.insert(0, str(PKG_PYTHON))

SUITES = ("detectors", "resolve", "tokenize", "canonical", "analyze",
          "sequence", "errors", "report", "cli")


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _load_input_data(inp: dict):
    """Resolve an analyze-shaped input: inline `data` or `input_file`."""
    if "data" in inp:
        return inp["data"]
    path = FIXTURES / inp["input_file"]
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Per-suite generators: input dict -> (expected, {golden_filename: text})
# ---------------------------------------------------------------------------

def gen_detectors(inp):
    from tryaii.cachelint import detectors

    findings = detectors.scan(inp["text"])
    first = detectors.first_blocking(findings)
    index = findings.index(first) if first is not None else None
    return {"findings": [f.to_dict() for f in findings],
            "first_blocking_index": index}, {}


def gen_resolve(inp):
    from tryaii.cachelint import providers

    try:
        resolved = providers.resolve(inp["provider"], inp["model"])
    except ValueError as exc:
        return {"error": str(exc)}, {}
    return {"provider_key": resolved["provider_key"],
            "min_tokens": resolved["min_tokens"],
            "tier_note": resolved["tier_note"],
            "warnings": resolved["warnings"],
            "upstream": resolved["upstream"]}, {}


def gen_tokenize(inp):
    from tryaii.cachelint import tokenizers

    tc = tokenizers.count_tokens(inp["text"], inp["provider_key"],
                                 inp.get("upstream"))
    return tc.to_dict(), {}


def gen_canonical(inp):
    from tryaii.cachelint import analyzer

    text, sections, non_text = analyzer.build_canonical(inp["prompt"])
    return {"text": text,
            "sections": [{"name": s.name, "start": s.start, "end": s.end}
                         for s in sections],
            "non_text": non_text}, {}


def gen_analyze(inp):
    from tryaii.cachelint import api

    return api.analyze(_load_input_data(inp)), {}


def gen_sequence(inp):
    from tryaii.cachelint import api

    return api.analyze(_load_input_data(inp))["sequences"], {}


def gen_errors(inp):
    from tryaii.cachelint import api

    try:
        api.analyze(_load_input_data(inp))
    except ValueError as exc:
        return {"error": str(exc)}, {}
    raise AssertionError("errors-suite case did not raise ValueError")


def gen_report(inp, case_name: str):
    from tryaii.cachelint import api, report

    text = report.render(api.analyze(_load_input_data(inp)))
    golden = case_name + ".golden.txt"
    return {"golden": golden}, {golden: text + "\n"}


def gen_cli(inp, case_name: str, parser_stderr: bool = False):
    argv = inp["argv"]
    env = dict(os.environ,
               TRYAII_NO_BANNER="1",
               PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(PKG_PYTHON))
    stdin_data = None
    if "stdin_file" in inp:
        stdin_data = (FIXTURES / inp["stdin_file"]).read_bytes()
    # cli() reads sys.argv[1:], which under `python -c` is exactly *argv —
    # the fixture argv therefore includes the "cachelint" subcommand itself.
    proc = subprocess.run(
        [sys.executable, "-c",
         "from tryaii.cli.main import cli; cli()", *argv],
        input=stdin_data, capture_output=True, cwd=str(FIXTURES), env=env,
    )
    stdout = proc.stdout.decode("utf-8").replace("\r\n", "\n")
    stderr = proc.stderr.decode("utf-8").replace("\r\n", "\n")
    golden = case_name + ".stdout.golden.txt"
    # Parser-generated usage text differs across languages and Python
    # versions — never freeze it (see schema.json stderr_parser_specific).
    return {"stdout_golden": golden,
            "stderr": None if parser_stderr else (stderr or None),
            "exit_code": proc.returncode}, {golden: stdout}


# ---------------------------------------------------------------------------

def run_suite(name: str) -> dict:
    """Return {relative_path: new_content_str} for every file this suite owns."""
    suite_dir = FIXTURES / name
    doc = json.loads((suite_dir / "cases.json").read_text(encoding="utf-8"))
    out_files: dict[str, str] = {}

    for case in doc["cases"]:
        inp = case["input"]
        if name == "report":
            expected, goldens = gen_report(inp, case["name"])
        elif name == "cli":
            expected, goldens = gen_cli(inp, case["name"],
                                        case.get("stderr_parser_specific", False))
        else:
            expected, goldens = globals()["gen_" + name](inp)
        case["expected"] = expected
        for fname, text in goldens.items():
            out_files[f"{name}/{fname}"] = text

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
