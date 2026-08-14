"""Cross-CLI byte-diff: the Python and Node `tryaii diagnose` must be twins.

For every cli-suite fixture this spawns BOTH CLIs (Python in-process module,
Node from packages/node/dist/cli.js), each in its own FRESH temp cwd with the
same corpus files staged, and asserts byte-identical stdout/stderr (after
\r\n normalization), equal exit codes, AND byte-identical WRITTEN run files
(inventory.json / findings.json / meta.json / latest) — diagnose writes a
run store, so the files are part of the contract, not just the stream.

Requires a built Node CLI: run `npm run build` in packages/node first. Skips
cleanly when dist/cli.js (or node itself) is absent, so python-ci — which
never builds Node — is unaffected. Documented as a pre-merge step in
shared/diagnose/SPEC.md §5.
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


def _run_in(cmd: list, stdin_data, env, cwd: Path,
            corpus: list) -> tuple[int, str, str]:
    for name in corpus:
        shutil.copy2(FIXTURES / "corpus" / name, cwd / name)
    proc = subprocess.run(cmd, input=stdin_data, capture_output=True,
                          cwd=str(cwd), env=env)
    return (proc.returncode,
            proc.stdout.decode("utf-8").replace("\r\n", "\n"),
            proc.stderr.decode("utf-8").replace("\r\n", "\n"))


def _written_files(cwd: Path, corpus: list) -> dict:
    """Every file the CLI wrote under this cwd (staged corpus files excluded),
    as {relative posix path: normalized bytes}."""
    out = {}
    for path in sorted(cwd.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(cwd).as_posix()
        if rel in corpus:
            continue
        out[rel] = path.read_bytes().replace(b"\r\n", b"\n")
    return out


@pytest.mark.parametrize("case", _cases())
def test_cli_outputs_byte_identical(case):
    if case["expected"] is None:
        pytest.skip("expected block not frozen yet")
    inp = case["input"]
    argv = inp["argv"]
    corpus = inp.get("copy_from_corpus", [])
    env = dict(os.environ, TRYAII_NO_BANNER="1", PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(REPO_ROOT / "packages" / "python"))
    stdin_data = None
    if "stdin_file" in inp:
        stdin_data = (FIXTURES / inp["stdin_file"]).read_bytes()

    with tempfile.TemporaryDirectory() as py_tmp, \
            tempfile.TemporaryDirectory() as node_tmp:
        py_cwd, node_cwd = Path(py_tmp), Path(node_tmp)
        py_code, py_out, py_err = _run_in(
            [sys.executable, "-c", "from tryaii.cli.main import cli; cli()", *argv],
            stdin_data, env, py_cwd, corpus)
        node_code, node_out, node_err = _run_in(
            [NODE_EXE, str(NODE_CLI), *argv], stdin_data, env, node_cwd, corpus)

        assert node_code == py_code, (
            f"exit codes differ: python={py_code} node={node_code}\n"
            f"python stderr: {py_err!r}\nnode stderr: {node_err!r}")
        assert node_out == py_out, "stdout differs between the two CLIs"
        assert node_err == py_err, "stderr differs between the two CLIs"

        py_files = _written_files(py_cwd, corpus)
        node_files = _written_files(node_cwd, corpus)
        assert sorted(py_files) == sorted(node_files), (
            "the two CLIs wrote different file sets")
        for rel, content in py_files.items():
            assert node_files[rel] == content, (
                f"written file differs between the two CLIs: {rel}")
