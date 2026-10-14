"""Cross-CLI byte-diff: the Python and Node `tryaii monitor` twins.

For every cli-suite fixture this spawns BOTH CLIs (Python in-process
module, Node from packages/node/dist/cli.js), each in its own FRESH temp
data dir with the same corpus stores staged under <tmp>/monitor/ (the
staging scripts/gen-monitor-fixtures.py uses) and the case's `env` merged
into the subprocess environment. Asserts byte-identical stdout/stderr/exit
codes AND every file under the data dir -- the staged stores plus whatever
the verb wrote (config.json for `set`).

Requires a built Node CLI: run `npm run build` in packages/node first.
Skips cleanly when dist/cli.js (or node itself) is absent. Documented as
a pre-merge step in shared/monitor/SPEC.md §5.
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
FIXTURES = REPO_ROOT / "shared" / "monitor" / "fixtures"
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


def _run_in(cmd_prefix: list, inp: dict, base_env: dict,
            cwd: Path) -> tuple[int, str, str]:
    """Stage the case's stores under <cwd>/monitor/, point the data dir at
    cwd, merge the case env (which may re-point it, e.g. to '.') and run."""
    env = dict(base_env, TRYAII_DRE_DATA_DIR=str(cwd))
    env.update(inp.get("env") or {})
    for name in inp.get("copy_store", []):
        shutil.copytree(FIXTURES / "corpus" / name, cwd / "monitor" / name)
    proc = subprocess.run([*cmd_prefix, *inp["argv"]],
                          capture_output=True, cwd=str(cwd), env=env)
    return (proc.returncode,
            proc.stdout.decode("utf-8").replace("\r\n", "\n"),
            proc.stderr.decode("utf-8").replace("\r\n", "\n"))


def _written_files(cwd: Path) -> dict:
    """Every file under this cwd as {relative posix path: normalized bytes}
    (staged corpus files included -- identical in both cwds, and a run that
    MODIFIES one is still compared)."""
    out = {}
    for path in sorted(cwd.rglob("*")):
        if not path.is_file():
            continue
        out[path.relative_to(cwd).as_posix()] = (
            path.read_bytes().replace(b"\r\n", b"\n"))
    return out


@pytest.mark.parametrize("case", _cases())
def test_cli_outputs_byte_identical(case):
    if case["expected"] is None:
        pytest.skip("expected block not frozen yet")
    inp = case["input"]
    base_env = dict(os.environ, TRYAII_NO_BANNER="1", PYTHONIOENCODING="utf-8",
                    PYTHONPATH=str(REPO_ROOT / "packages" / "python"))

    with tempfile.TemporaryDirectory() as py_tmp, \
            tempfile.TemporaryDirectory() as node_tmp:
        py_cwd, node_cwd = Path(py_tmp), Path(node_tmp)
        py_code, py_out, py_err = _run_in(
            [sys.executable, "-c", "from tryaii.cli.main import cli; cli()"],
            inp, base_env, py_cwd)
        node_code, node_out, node_err = _run_in(
            [NODE_EXE, str(NODE_CLI)], inp, base_env, node_cwd)

        assert node_code == py_code, (
            f"exit codes differ: python={py_code} node={node_code}\n"
            f"python stderr: {py_err!r}\nnode stderr: {node_err!r}")
        assert node_out == py_out, "stdout differs between the two CLIs"
        assert node_err == py_err, "stderr differs between the two CLIs"

        py_files = _written_files(py_cwd)
        node_files = _written_files(node_cwd)
        assert sorted(py_files) == sorted(node_files), (
            "the two CLIs wrote different file sets")
        for rel, content in py_files.items():
            assert node_files[rel] == content, (
                f"written file differs between the two CLIs: {rel}")
