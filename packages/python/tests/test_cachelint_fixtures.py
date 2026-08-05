"""Golden-fixture conformance for the cachelint engine.

The fixtures in shared/cachelint/fixtures/ are the frozen cross-SDK behavior
contract (SPEC.md §5): generated from THIS engine by
scripts/gen-cachelint-fixtures.py, hand-reviewed, and byte-matched by the Node
SDK's cachelint-fixtures.test.ts. This suite proves the Python engine still
reproduces every frozen `expected` block — any intentional behavior change
must regenerate the fixtures (and update the TS port) in the same PR.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "shared" / "cachelint" / "fixtures"

pytestmark = pytest.mark.skipif(
    not FIXTURES.exists(),
    reason="shared/cachelint/fixtures not present (package-only checkout)",
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


@pytest.mark.parametrize("case", _cases("detectors"))
def test_detectors(case):
    from tryaii.cachelint import detectors

    expected = _require_frozen(case)
    findings = detectors.scan(case["input"]["text"])
    first = detectors.first_blocking(findings)
    assert [f.to_dict() for f in findings] == expected["findings"]
    index = findings.index(first) if first is not None else None
    assert index == expected["first_blocking_index"]


@pytest.mark.parametrize("case", _cases("resolve"))
def test_resolve(case):
    from tryaii.cachelint import providers

    expected = _require_frozen(case)
    if "error" in expected:
        with pytest.raises(ValueError) as excinfo:
            providers.resolve(case["input"]["provider"], case["input"]["model"])
        assert str(excinfo.value) == expected["error"]
        return
    resolved = providers.resolve(case["input"]["provider"], case["input"]["model"])
    got = {k: resolved[k]
           for k in ("provider_key", "min_tokens", "tier_note", "warnings", "upstream")}
    assert got == expected


@pytest.mark.parametrize("case", _cases("tokenize"))
def test_tokenize(case):
    from tryaii.cachelint import tokenizers

    expected = _require_frozen(case)
    tc = tokenizers.count_tokens(
        case["input"]["text"], case["input"]["provider_key"], case["input"].get("upstream"))
    assert tc.to_dict() == expected


@pytest.mark.parametrize("case", _cases("canonical"))
def test_canonical(case):
    from tryaii.cachelint import analyzer

    expected = _require_frozen(case)
    text, sections, non_text = analyzer.build_canonical(case["input"]["prompt"])
    assert text == expected["text"]
    assert [{"name": s.name, "start": s.start, "end": s.end}
            for s in sections] == expected["sections"]
    assert non_text == expected["non_text"]


@pytest.mark.parametrize("case", _cases("analyze"))
def test_analyze(case):
    from tryaii.cachelint import api

    expected = _require_frozen(case)
    assert api.analyze(_load_input_data(case["input"])) == expected


@pytest.mark.parametrize("case", _cases("sequence"))
def test_sequence(case):
    from tryaii.cachelint import api

    expected = _require_frozen(case)
    assert api.analyze(_load_input_data(case["input"]))["sequences"] == expected


@pytest.mark.parametrize("case", _cases("errors"))
def test_errors(case):
    from tryaii.cachelint import api

    expected = _require_frozen(case)
    with pytest.raises(ValueError) as excinfo:
        api.analyze(_load_input_data(case["input"]))
    assert str(excinfo.value) == expected["error"]


@pytest.mark.parametrize("case", _cases("report"))
def test_report(case):
    from tryaii.cachelint import api, report

    expected = _require_frozen(case)
    golden = (FIXTURES / "report" / expected["golden"]).read_text(encoding="utf-8")
    rendered = report.render(api.analyze(_load_input_data(case["input"]))) + "\n"
    assert rendered == golden


@pytest.mark.parametrize("case", _cases("cli"))
def test_cli(case):
    """Runs the real `tryaii cachelint` subcommand as a subprocess with the
    fixtures dir as cwd (argv paths and error messages are relative to it)."""
    expected = _require_frozen(case)
    env = dict(os.environ,
               TRYAII_NO_BANNER="1",
               PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(REPO_ROOT / "packages" / "python"))
    stdin_data = None
    if "stdin_file" in case["input"]:
        stdin_data = (FIXTURES / case["input"]["stdin_file"]).read_bytes()
    proc = subprocess.run(
        [sys.executable, "-c",
         "from tryaii.cli.main import cli; cli()", *case["input"]["argv"]],
        input=stdin_data, capture_output=True, cwd=str(FIXTURES), env=env,
    )
    stdout = proc.stdout.decode("utf-8").replace("\r\n", "\n")
    stderr = proc.stderr.decode("utf-8").replace("\r\n", "\n")
    golden = (FIXTURES / "cli" / expected["stdout_golden"]).read_text(encoding="utf-8")
    assert proc.returncode == expected["exit_code"]
    assert stdout == golden
    if case.get("stderr_parser_specific"):
        # argparse/parseArgs usage text differs per language and Python
        # version -- only require that SOME error text was shown.
        assert stderr
    else:
        assert stderr == (expected["stderr"] or "")
