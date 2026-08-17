"""The resumable orchestrator: one advance() per CLI invocation
(SPEC.md §2, §4).

Clock-free and network-seamed: now/stamp/version arrive via opts, the
diagnose-store reader and the sender are injectable. Usage-level mistakes
raise ValueError (the CLI maps them to exit 2); everything else reports a
stage and exits 0 — enrollment never blocks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from tryaii.cachelint._jsonutil import normalize_numbers
from tryaii.designpartner import transport
from tryaii.designpartner.catalog import (
    all_questions,
    applicable_questions,
    load_catalog,
    tiers_by_id,
    validate_answers,
)
from tryaii.designpartner.payload import build_submission
from tryaii.designpartner.state import (
    PREVIEW_FILE,
    derive_stage,
    load_state,
    new_state,
    save_state,
)

DEFAULT_DIAGNOSE_OUT_DIR = ".tryaii/diagnose"

_NEXT = {
    "questionnaire": {
        "description": "Interview the user from the questionnaire block "
                       "(honor ask_if; every answer comes from the user), "
                       "then save their answers.",
        "command": "tryaii designpartner --answers answers.json",
    },
    "diagnose": {
        "description": "The chosen tier requires a diagnose run. Follow the "
                       "tryaii-diagnose skill (or run 'tryaii diagnose "
                       "check'), then re-run designpartner.",
        "command": "tryaii designpartner",
    },
    "consent": {
        "description": "Show the user each consent tier's copy VERBATIM; "
                       "they choose one explicitly.",
        "command": "tryaii designpartner --consent <tier_id>",
    },
    "confirm": {
        "description": "Show the user the preview (exactly what will be "
                       "sent). Only after they explicitly agree, confirm.",
        "command": "tryaii designpartner --confirm",
    },
    "submitted": {
        "description": "Enrollment complete. Nothing further to do.",
        "command": None,
    },
}


def _default_load_latest_diagnose(diagnose_out_dir) -> Optional[dict]:
    from tryaii.diagnose.store import (
        latest_run_id,
        load_run_findings,
        load_run_inventory,
    )

    run_id = latest_run_id(Path(diagnose_out_dir))
    if run_id is None:
        return None
    findings = load_run_findings(Path(diagnose_out_dir), run_id)
    return {
        "run_id": run_id,
        "summary": findings["summary"],
        "findings": findings,
        "inventory": load_run_inventory(Path(diagnose_out_dir), run_id),
    }


def _display_path(out_dir, name: str) -> str:
    return f"{str(out_dir)}/{name}".replace("\\", "/")


def _write_doc(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def advance(
    out_dir,
    inputs: dict,
    opts: dict,
    *,
    diagnose_out_dir=DEFAULT_DIAGNOSE_OUT_DIR,
    load_latest_diagnose: Optional[Callable[[], Optional[dict]]] = None,
    send: Optional[Callable[[str, bytes], bool]] = None,
) -> dict:
    """One invocation: ingest inputs, advance state, return the status
    report (tryaii.designpartner.status/1). inputs: {answers: obj|None,
    consent: tier|None, confirm: bool, reset: bool} — at most one action
    (the CLI enforces this as a usage error). opts: {now, stamp, version,
    url}."""
    catalog = load_catalog()
    tiers = tiers_by_id(catalog)
    now = opts.get("now")
    version = opts.get("version") or "0.0.0"
    url = opts.get("url") or transport.effective_url()
    out_path = Path(out_dir)

    if load_latest_diagnose is None:
        def load_latest_diagnose():
            return _default_load_latest_diagnose(diagnose_out_dir)
    if send is None:
        def send(target_url: str, body: bytes) -> bool:
            return transport.send_submission(target_url, body, version=version)

    action: dict[str, Any]

    # --- reset -----------------------------------------------------------
    if inputs.get("reset"):
        removed = []
        for name in ("state.json", PREVIEW_FILE):
            path = out_path / name
            if path.is_file():
                path.unlink()
                removed.append(_display_path(out_dir, name))
        action = {"type": "reset", "removed": removed}
        report = {
            "schema": "tryaii.designpartner.status/1",
            "stage": "questionnaire",
            "updated_at": now,
            "tool": {"name": "tryaii", "version": version},
            "action": action,
            "next": {"description": "Start over from the beginning.",
                     "command": "tryaii designpartner"},
        }
        return normalize_numbers(report)

    # --- load / enroll ---------------------------------------------------
    state = load_state(out_dir)
    is_new = state is None
    if is_new:
        state = new_state(now, version)

    # --- ingest the action input ----------------------------------------
    if inputs.get("answers") is not None:
        result = validate_answers(catalog, inputs["answers"])
        if result["problems"]:
            action = {"type": "answers_rejected",
                      "problems": result["problems"],
                      "warnings": result["warnings"]}
        else:
            state["answers"] = result["answers"]
            action = {"type": "answers_saved",
                      "count": len(result["answers"]),
                      "warnings": result["warnings"]}
    elif inputs.get("consent") is not None:
        tier_id = inputs["consent"]
        if tier_id not in tiers:
            raise ValueError(
                f"unknown consent tier: {tier_id}. Valid tiers: "
                + ", ".join(t["id"] for t in catalog["consent_tiers"]))
        docs = load_latest_diagnose()
        state["consent"] = {
            "tier": tier_id,
            "chosen_at": now,
            "diagnose_run_id": docs["run_id"] if docs is not None else None,
        }
        # A new consent cycle clears the stored submission block (the
        # submission FILES on disk are kept as records).
        state["submission"] = None
        action = {"type": "consent_chosen", "tier": tier_id}
    elif inputs.get("confirm"):
        action = _confirm(state, catalog, tiers, out_dir, out_path, url,
                          now, version, opts, load_latest_diagnose, send)
    elif is_new:
        action = {"type": "enrolled"}
    else:
        action = {"type": "status"}

    # --- derive stage + refresh the preview ------------------------------
    docs = load_latest_diagnose()
    has_run = docs is not None
    stage = derive_stage(state, has_run, tiers)
    state["stage"] = stage
    state["updated_at"] = now
    save_state(out_dir, state)

    preview_block = None
    if stage == "confirm":
        # Regenerated on EVERY entry — the preview can never be stale when
        # shown (SPEC §2).
        tier = tiers[state["consent"]["tier"]]
        preview_docs = None if state["consent"]["tier"] == "contact_only" else docs
        preview = build_submission(state, preview_docs, version=version,
                                   submitted_at=None, confirmed_at=None)
        _write_doc(out_path / PREVIEW_FILE, preview)
        preview_block = {
            "path": _display_path(out_dir, PREVIEW_FILE),
            "tier": state["consent"]["tier"],
            "includes": tier["includes"],
            "answers_count": len(state["answers"]),
            "diagnose_run_id": docs["run_id"] if docs is not None else None,
        }

    # --- report ----------------------------------------------------------
    report: dict[str, Any] = {
        "schema": "tryaii.designpartner.status/1",
        "stage": stage,
        "updated_at": now,
        "tool": {"name": "tryaii", "version": version},
        "action": action,
        "next": dict(_NEXT[stage]),
    }
    if stage == "questionnaire":
        answers = state["answers"] or {}
        applicable = applicable_questions(catalog, answers)
        applicable_set = set(applicable)
        report["questionnaire"] = {
            "sections": catalog["sections"],
            "applicable": applicable,
            "required": [q["id"] for q in all_questions(catalog)
                         if q["required"] and q["id"] in applicable_set],
            "answers": answers,
        }
    if stage in ("consent", "diagnose"):
        chosen = state["consent"]["tier"] if state["consent"] is not None else None
        report["consent"] = {
            "tiers": catalog["consent_tiers"],
            "chosen": chosen,
            "requires_diagnose_unmet": stage == "diagnose",
        }
    if stage == "confirm":
        report["preview"] = preview_block
    if stage == "submitted":
        submission = state["submission"]
        report["submission"] = {
            "path": submission["path"],
            "delivered": submission["delivered"],
            "url": submission["url"],
            "submitted_at": submission["submitted_at"],
        }
    return normalize_numbers(report)


def _confirm(state, catalog, tiers, out_dir, out_path: Path, url: str,
             now, version: str, opts: dict,
             load_latest_diagnose, send) -> dict:
    """--confirm: freshness-guarded build + save + send (SPEC §2, §5)."""
    docs = load_latest_diagnose()
    stage = derive_stage(state, docs is not None, tiers)
    if stage != "confirm":
        raise ValueError(
            "nothing to confirm — run 'tryaii designpartner' to see the "
            "current stage")

    preview_path = out_path / PREVIEW_FILE
    if not preview_path.is_file():
        raise ValueError(
            "nothing to confirm — run 'tryaii designpartner' to generate "
            "the preview first")
    on_disk = json.loads(preview_path.read_text(encoding="utf-8"))
    tier_id = state["consent"]["tier"]
    expected_docs = None if tier_id == "contact_only" else docs
    expected = build_submission(state, expected_docs, version=version,
                                submitted_at=None, confirmed_at=None)
    if on_disk != expected:
        raise ValueError(
            "preview is stale — run 'tryaii designpartner' to regenerate it")

    stamp = opts.get("stamp") or "submission"
    submission = build_submission(state, expected_docs, version=version,
                                  submitted_at=now, confirmed_at=now)
    name = f"submission-{stamp}.json"
    _write_doc(out_path / name, submission)
    body = (out_path / name).read_bytes()
    delivered = bool(send(url, body))

    state["submission"] = {
        "stamp": stamp,
        "submitted_at": now,
        "delivered": delivered,
        "url": url,
        "path": _display_path(out_dir, name),
    }
    return {"type": "submitted", "delivered": delivered}
