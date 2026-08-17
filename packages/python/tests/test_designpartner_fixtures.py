"""Golden-fixture conformance for the designpartner engine.

The fixtures in shared/designpartner/fixtures/ are the frozen cross-SDK
behavior contract (shared/designpartner/SPEC.md §7): generated from THIS
engine by scripts/gen-designpartner-fixtures.py, hand-reviewed, and
byte-matched by the Node SDK's designpartner-fixtures.test.ts. Any
intentional behavior change must regenerate the fixtures (and update the
TS port) in the same PR.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "shared" / "designpartner" / "fixtures"

pytestmark = pytest.mark.skipif(
    not FIXTURES.exists(),
    reason="shared/designpartner/fixtures not present (package-only checkout)",
)


def _cases(suite: str) -> list:
    path = FIXTURES / suite / "cases.json"
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [pytest.param(c, id=c["name"]) for c in doc["cases"]]


def _load(inp: dict, key: str):
    if key in inp:
        return inp[key]
    return json.loads((FIXTURES / inp[f"{key}_file"]).read_text(encoding="utf-8"))


def _require_frozen(case: dict) -> dict:
    if case["expected"] is None:
        pytest.skip("expected block not frozen yet")
    return case["expected"]


@pytest.mark.parametrize("case", _cases("catalog"))
def test_catalog(case):
    from tryaii.designpartner.catalog import (
        all_questions,
        applicable_questions,
        load_catalog,
    )

    expected = _require_frozen(case)
    catalog = load_catalog()
    answers = _load(case["input"], "answers")
    applicable = applicable_questions(catalog, answers)
    applicable_set = set(applicable)
    required = [q["id"] for q in all_questions(catalog)
                if q["required"] and q["id"] in applicable_set]
    assert {"applicable": applicable, "required": required} == expected


@pytest.mark.parametrize("case", _cases("validate"))
def test_validate(case):
    from tryaii.cachelint._jsonutil import normalize_numbers
    from tryaii.designpartner.catalog import load_catalog, validate_answers

    expected = _require_frozen(case)
    result = normalize_numbers(
        validate_answers(load_catalog(), _load(case["input"], "answers")))
    assert result == expected
    # Key order is part of the contract — pin via serialized comparison too.
    assert json.dumps(result, ensure_ascii=False) == json.dumps(
        expected, ensure_ascii=False)


@pytest.mark.parametrize("case", _cases("payload"))
def test_payload(case):
    from tryaii.designpartner.payload import build_submission

    expected = _require_frozen(case)
    inp = case["input"]
    diagnose_docs = None
    if "diagnose_docs" in inp or "diagnose_docs_file" in inp:
        diagnose_docs = _load(inp, "diagnose_docs")
    result = build_submission(
        {"answers": _load(inp, "answers"), "consent": inp["consent"]},
        diagnose_docs,
        version=inp.get("version", "0.0.0"),
        submitted_at=inp.get("submitted_at"),
        confirmed_at=inp.get("confirmed_at"),
    )
    assert result == expected
    assert json.dumps(result, ensure_ascii=False) == json.dumps(
        expected, ensure_ascii=False)


@pytest.mark.parametrize("case", _cases("state"))
def test_state(case):
    from tryaii.designpartner.catalog import load_catalog, tiers_by_id
    from tryaii.designpartner.state import derive_stage

    expected = _require_frozen(case)
    stage = derive_stage(case["input"]["state"],
                         case["input"]["has_diagnose_run"],
                         tiers_by_id(load_catalog()))
    assert {"stage": stage} == expected


@pytest.mark.parametrize("case", _cases("cli"))
def test_cli(case):
    """Runs the real `tryaii designpartner` command as a subprocess in a
    FRESH temp cwd; the case's `env` object is merged into the subprocess
    environment (the unroutable-URL seam)."""
    expected = _require_frozen(case)
    inp = case["input"]
    env = dict(os.environ,
               TRYAII_NO_BANNER="1",
               PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(REPO_ROOT / "packages" / "python"))
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
        proc = subprocess.run(
            [sys.executable, "-c",
             "from tryaii.cli.main import cli; cli()", *inp["argv"]],
            input=stdin_data, capture_output=True, cwd=tmp, env=env,
        )
    stdout = proc.stdout.decode("utf-8").replace("\r\n", "\n")
    stderr = proc.stderr.decode("utf-8").replace("\r\n", "\n")
    golden = (FIXTURES / "cli" / expected["stdout_golden"]).read_text(encoding="utf-8")
    assert proc.returncode == expected["exit_code"]
    assert stdout == golden
    assert stderr == (expected["stderr"] or "")
