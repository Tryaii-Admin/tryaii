"""Golden-fixture conformance for the monitor engine.

The fixtures in shared/monitor/fixtures/ are the frozen cross-SDK behavior
contract (shared/monitor/SPEC.md §4): generated from THIS engine by
scripts/gen-monitor-fixtures.py, hand-reviewed, and byte-matched by the Node
SDK's monitor-fixtures.test.ts. This suite proves the Python engine still
reproduces every frozen `expected` block, by running the generator's own
per-suite functions, so the generator and the tests can never disagree.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "shared" / "monitor" / "fixtures"
GENERATOR = REPO_ROOT / "scripts" / "gen-monitor-fixtures.py"

pytestmark = pytest.mark.skipif(
    not FIXTURES.exists() or not GENERATOR.exists(),
    reason="shared/monitor/fixtures not present (package-only checkout)",
)

ENGINE_SUITES = ("extract", "resolve", "winner", "pin", "config", "status", "cache",
                 "cache_usage")


def _generator():
    spec = importlib.util.spec_from_file_location("gen_monitor_fixtures", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cases(suite: str) -> list:
    path = FIXTURES / suite / "cases.json"
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [pytest.param(suite, c, id=f"{suite}/{c['name']}") for c in doc["cases"]]


def _all_cases() -> list:
    out = []
    for suite in ENGINE_SUITES:
        out.extend(_cases(suite))
    return out


@pytest.mark.parametrize("suite,case", _all_cases())
def test_frozen_expected(suite, case):
    if case["expected"] is None:
        pytest.skip("expected block not frozen yet")
    gen = getattr(_generator(), "gen_" + suite)
    actual = gen(case["input"])
    # Key order is contract (SPEC §1.2): compare the serialized form too.
    assert actual == case["expected"]
    assert (json.dumps(actual, ensure_ascii=False)
            == json.dumps(case["expected"], ensure_ascii=False))


def test_every_engine_suite_has_cases():
    for suite in ENGINE_SUITES:
        assert (FIXTURES / suite / "cases.json").exists(), suite
        assert _cases(suite), f"{suite} has no cases"


# Re-runs the whole fixture generator (~35 s): full CI only. The fixture
# cases themselves (the cross-SDK contract) run in the light suite.
@pytest.mark.slow
def test_generator_check_is_clean():
    """`--check` must be clean: the frozen files match the engine."""
    module = _generator()
    assert module.main(["--check"]) == 0
