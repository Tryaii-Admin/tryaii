"""Golden-fixture conformance for the diagnose engine.

The fixtures in shared/diagnose/fixtures/ are the frozen cross-SDK behavior
contract (shared/diagnose/SPEC.md §5): generated from THIS engine by
scripts/gen-diagnose-fixtures.py, hand-reviewed, and byte-matched by the Node
SDK's diagnose-fixtures.test.ts. This suite proves the Python engine still
reproduces every frozen `expected` block — any intentional behavior change
must regenerate the fixtures (and update the TS port) in the same PR.
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
FIXTURES = REPO_ROOT / "shared" / "diagnose" / "fixtures"

pytestmark = pytest.mark.skipif(
    not FIXTURES.exists(),
    reason="shared/diagnose/fixtures not present (package-only checkout)",
)


def _cases(suite: str) -> list:
    path = FIXTURES / suite / "cases.json"
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [pytest.param(c, id=c["name"]) for c in doc["cases"]]


def _load_input_data(inp: dict):
    if "data" in inp:
        return inp["data"]
    return json.loads((FIXTURES / inp["input_file"]).read_text(encoding="utf-8"))


def _require_frozen(case: dict) -> dict:
    if case["expected"] is None:
        pytest.skip("expected block not frozen yet")
    return case["expected"]


def _registry():
    from tryaii.registry.models import ModelRegistry

    return ModelRegistry.default()


def _priorities(inp: dict):
    from tryaii.scoring.priorities import Priorities

    return Priorities.from_dict(inp.get("priorities") or {})


@pytest.mark.parametrize("case", _cases("intake"))
def test_intake(case):
    from tryaii.cachelint._jsonutil import normalize_numbers
    from tryaii.diagnose.intake import normalize_inventory

    expected = _require_frozen(case)
    data = _load_input_data(case["input"])
    if "error" in expected:
        with pytest.raises(ValueError) as excinfo:
            normalize_inventory(data)
        assert str(excinfo.value) == expected["error"]
        return
    assert normalize_numbers(normalize_inventory(data)) == expected


@pytest.mark.parametrize("case", _cases("resolve"))
def test_resolve(case):
    from tryaii.diagnose.resolve import resolve_model_id

    expected = _require_frozen(case)
    model_id, method = resolve_model_id(case["input"].get("model"), _registry())
    assert {"model_id": model_id, "method": method} == expected


@pytest.mark.parametrize("case", _cases("modelfit"))
def test_modelfit(case):
    from tryaii.cachelint._jsonutil import normalize_numbers
    from tryaii.diagnose.modelfit import run_model_fit

    expected = _require_frozen(case)
    inp = case["input"]
    payload, _internal = run_model_fit(
        inp["classification"], inp.get("resolved_model_id"),
        inp.get("declared_model"), _priorities(inp), _registry())
    assert normalize_numbers(payload) == expected


@pytest.mark.parametrize("case", _cases("cost"))
def test_cost(case):
    from tryaii.cachelint._jsonutil import normalize_numbers
    from tryaii.diagnose.api import _read_discount_factors
    from tryaii.diagnose.cost import run_cost
    from tryaii.diagnose.modelfit import run_model_fit

    expected = _require_frozen(case)
    inp = case["input"]
    fit_internal = None
    if "classification" in inp:
        _payload, fit_internal = run_model_fit(
            inp["classification"], inp.get("resolved_model_id"),
            inp.get("declared_model"), _priorities(inp), _registry())
    result = run_cost(
        canonical_text=inp.get("canonical_text"),
        resolved_model_id=inp.get("resolved_model_id"),
        declared_model=inp.get("declared_model"),
        registry=_registry(),
        output_tokens=inp.get("output_tokens", 500),
        calls_per_day=inp.get("calls_per_day"),
        cache_ctx=inp.get("cache_ctx"),
        fit_internal=fit_internal,
        read_discount_factors=_read_discount_factors(),
    )
    assert normalize_numbers(result) == expected


@pytest.mark.parametrize("case", _cases("hygiene"))
def test_hygiene(case):
    from tryaii.cachelint._jsonutil import normalize_numbers
    from tryaii.diagnose.hygiene import run_hygiene

    expected = _require_frozen(case)
    assert normalize_numbers(run_hygiene(case["input"]["prompt"])) == expected


@pytest.mark.parametrize("case", _cases("check"))
def test_check(case):
    from tryaii.diagnose.api import analyze_inventory

    expected = _require_frozen(case)
    data = _load_input_data(case["input"])
    opts = case["input"].get("opts") or {}
    if "error" in expected:
        with pytest.raises(ValueError) as excinfo:
            analyze_inventory(data, opts)
        assert str(excinfo.value) == expected["error"]
        return
    result = analyze_inventory(data, opts)
    assert result == expected
    # Key order is part of the contract (SPEC §1.3) — the Node suite pins it
    # via JSON.stringify; pin it here via a serialized comparison too.
    assert json.dumps(result, ensure_ascii=False) == json.dumps(
        expected, ensure_ascii=False)


def _report_findings(inp: dict) -> dict:
    from tryaii.diagnose.api import analyze_inventory

    data = json.loads(
        (FIXTURES / inp["inventory_file"]).read_text(encoding="utf-8"))
    return analyze_inventory(data, inp.get("opts") or {})


@pytest.mark.parametrize("case", _cases("report"))
def test_report(case):
    """Report fixtures render findings PRODUCED BY THE ENGINE, so engine
    changes regenerate check and report suites consistently."""
    from tryaii.diagnose.report import render_report_html

    expected = _require_frozen(case)
    inp = case["input"]
    findings = _report_findings(inp)
    previous = _report_findings(inp["previous"]) if "previous" in inp else None
    golden = (FIXTURES / "report" / expected["golden"]).read_text(encoding="utf-8")
    assert render_report_html(findings, previous) == golden


@pytest.mark.parametrize("case", _cases("cli"))
def test_cli(case):
    """Runs the real `tryaii diagnose` subcommand as a subprocess in a FRESH
    temp cwd (`check` writes a run dir, so cli cases never run inside the
    fixtures tree; corpus inputs are staged via copy_from_corpus and
    pre_argv commands run first in the same cwd)."""
    expected = _require_frozen(case)
    inp = case["input"]
    env = dict(os.environ,
               TRYAII_NO_BANNER="1",
               PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(REPO_ROOT / "packages" / "python"))
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
