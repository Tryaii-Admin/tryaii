"""Run persistence (SPEC.md §4): .tryaii/diagnose/<run-id>/ + latest pointer.

The `latest` pointer is a plain text file (never a symlink — Windows).
Run ids sort lexicographically, so "previous run" is a plain sorted lookup.
All files are written with \\n newlines for cross-platform byte parity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

LATEST_FILE = "latest"


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_run(out_dir: Path, inventory_data, findings: dict) -> dict:
    """Persist one run; returns {name: Path} of everything written."""
    out_dir = Path(out_dir)
    run_id = findings["run_id"]
    run_dir = out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    meta = {"run_id": run_id,
            "generated_at": findings["generated_at"],
            "tool": findings["tool"]}
    paths = {
        "inventory": run_dir / "inventory.json",
        "findings": run_dir / "findings.json",
        "meta": run_dir / "meta.json",
        "latest": out_dir / LATEST_FILE,
    }
    _write(paths["inventory"], _dump(inventory_data))
    _write(paths["findings"], _dump(findings))
    _write(paths["meta"], _dump(meta))
    _write(paths["latest"], run_id + "\n")
    return paths


def list_run_ids(out_dir: Path) -> list[str]:
    """Run dirs (containing a findings.json), sorted ascending."""
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return []
    return sorted(
        p.name for p in out_dir.iterdir()
        if p.is_dir() and (p / "findings.json").is_file())


def latest_run_id(out_dir: Path) -> Optional[str]:
    """The pointer file's run id, falling back to the newest run dir."""
    pointer = Path(out_dir) / LATEST_FILE
    if pointer.is_file():
        run_id = pointer.read_text(encoding="utf-8").strip()
        if run_id and (Path(out_dir) / run_id / "findings.json").is_file():
            return run_id
    runs = list_run_ids(out_dir)
    return runs[-1] if runs else None


def previous_run_id(out_dir: Path, run_id: str) -> Optional[str]:
    """The run immediately before `run_id` in lexicographic order."""
    earlier = [r for r in list_run_ids(out_dir) if r < run_id]
    return earlier[-1] if earlier else None


def load_run_findings(out_dir: Path, run_id: str) -> dict:
    path = Path(out_dir) / run_id / "findings.json"
    return json.loads(path.read_text(encoding="utf-8"))
