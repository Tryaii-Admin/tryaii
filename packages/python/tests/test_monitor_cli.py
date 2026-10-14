"""`tryaii monitor` CLI goldens (shared/monitor/SPEC.md §5, fixture suite `cli`).

Each case spawns the Python CLI in a fresh temp data dir with the named
corpus stores staged under <tmp>/monitor/, through the generator's own
`run_cli_case`, and byte-compares stdout against the frozen golden, plus
stderr and the exit code. The Node CLI is held to the same goldens by
packages/node tests and to byte-identical written files by
test_monitor_cli_parity.py.
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
    not (FIXTURES / "cli" / "cases.json").exists() or not GENERATOR.exists(),
    reason="shared/monitor/fixtures/cli not present (package-only checkout)",
)


def _generator():
    spec = importlib.util.spec_from_file_location("gen_monitor_fixtures", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cases() -> list:
    path = FIXTURES / "cli" / "cases.json"
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [pytest.param(c, id=c["name"]) for c in doc["cases"]]


@pytest.mark.parametrize("case", _cases())
def test_cli_case_matches_golden(case):
    if case.get("expected") is None:
        pytest.skip("expected block not frozen yet")
    expected = case["expected"]
    proc = _generator().run_cli_case(case["input"])
    stdout = proc.stdout.decode("utf-8").replace("\r\n", "\n")
    stderr = proc.stderr.decode("utf-8").replace("\r\n", "\n")
    golden = (FIXTURES / "cli" / expected["stdout_golden"]).read_text(encoding="utf-8")
    assert proc.returncode == expected["exit_code"], stderr
    assert stdout == golden
    assert (stderr or None) == expected["stderr"]


def test_help_topic_and_verb_pages():
    """`tryaii help monitor` and `tryaii monitor <verb> --help` print the
    frozen constants (the cross-SDK byte check lives in test_parity.py)."""
    from tryaii.cli.main import HELP_MONITOR, MONITOR_VERB_HELP

    gen = _generator()
    proc = gen.run_cli_case({"argv": ["help", "monitor"]})
    assert proc.returncode == 0
    assert proc.stdout.decode("utf-8").replace("\r\n", "\n") == HELP_MONITOR
    for verb, text in MONITOR_VERB_HELP.items():
        proc = gen.run_cli_case({"argv": ["monitor", verb, "--help"]})
        assert proc.returncode == 0, verb
        assert proc.stdout.decode("utf-8").replace("\r\n", "\n") == text
