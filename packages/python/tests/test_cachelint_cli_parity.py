"""Cross-CLI byte-diff: the Python and Node `tryaii cachelint` must be twins.

For every cli-suite fixture this spawns BOTH CLIs (Python in-process module,
Node from packages/node/dist/cli.js) with the fixtures dir as cwd and asserts
byte-identical stdout (after \r\n normalization) and equal exit codes — the
strongest cross-SDK guarantee in the repo: not just equal data, equal bytes.

Requires a built Node CLI: run `npm run build` in packages/node first. Skips
cleanly when dist/cli.js (or node itself) is absent, so python-ci — which
never builds Node — is unaffected. Documented as a pre-merge step in
shared/cachelint/SPEC.md §5.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "shared" / "cachelint" / "fixtures"
NODE_CLI = REPO_ROOT / "packages" / "node" / "dist" / "cli.js"
NODE_EXE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    not FIXTURES.exists() or not NODE_CLI.exists() or NODE_EXE is None,
    reason="needs shared fixtures + a built Node CLI (npm run build) + node on PATH",
)


def _cases() -> list:
    path = FIXTURES / "cli" / "cases.json"
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [pytest.param(c, id=c["name"]) for c in doc["cases"]]


def _run(cmd: list, stdin_data, env) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, input=stdin_data, capture_output=True,
                          cwd=str(FIXTURES), env=env)
    return (proc.returncode,
            proc.stdout.decode("utf-8").replace("\r\n", "\n"),
            proc.stderr.decode("utf-8").replace("\r\n", "\n"))


@pytest.mark.parametrize("case", _cases())
def test_cli_outputs_byte_identical(case):
    if case["expected"] is None:
        pytest.skip("expected block not frozen yet")
    argv = case["input"]["argv"]
    env = dict(os.environ, TRYAII_NO_BANNER="1", PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(REPO_ROOT / "packages" / "python"))
    stdin_data = None
    if "stdin_file" in case["input"]:
        stdin_data = (FIXTURES / case["input"]["stdin_file"]).read_bytes()

    py_code, py_out, py_err = _run(
        [sys.executable, "-c", "from tryaii.cli.main import cli; cli()", *argv],
        stdin_data, env)
    node_code, node_out, node_err = _run(
        [NODE_EXE, str(NODE_CLI), *argv], stdin_data, env)

    assert node_code == py_code, (
        f"exit codes differ: python={py_code} node={node_code}\n"
        f"python stderr: {py_err!r}\nnode stderr: {node_err!r}")
    assert node_out == py_out, "stdout differs between the two CLIs"
    if case.get("stderr_parser_specific"):
        assert py_err and node_err   # both showed SOME usage error
    else:
        assert node_err == py_err, "stderr differs between the two CLIs"
