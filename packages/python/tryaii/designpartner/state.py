"""Enrollment state: load/save + stage derivation (SPEC.md §2, §4).

The stage is DERIVED from state facts plus one external fact (whether a
diagnose run exists) — the stored `stage` field is informational only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

STATE_FILE = "state.json"
PREVIEW_FILE = "preview.json"

STAGES = ("questionnaire", "diagnose", "consent", "confirm", "submitted")


def new_state(now, version: str) -> dict:
    return {
        "schema": "tryaii.designpartner.state/1",
        "created_at": now,
        "updated_at": now,
        "tool": {"name": "tryaii", "version": version},
        "stage": "questionnaire",
        "answers": None,
        "consent": None,
        "submission": None,
    }


def load_state(out_dir) -> Optional[dict]:
    path = Path(out_dir) / STATE_FILE
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(out_dir, state: dict) -> Path:
    path = Path(out_dir) / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    return path


def derive_stage(state: dict, has_diagnose_run: bool, tiers: dict) -> str:
    """SPEC §2 — answers -> consent -> (diagnose gate) -> confirm -> submitted."""
    if state["answers"] is None:
        return "questionnaire"
    if state["consent"] is None:
        return "consent"
    tier = tiers[state["consent"]["tier"]]
    if tier["requires_diagnose"] and not has_diagnose_run:
        return "diagnose"
    if state["submission"] is None:
        return "confirm"
    return "submitted"
