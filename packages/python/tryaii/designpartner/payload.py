"""Submission assembly (SPEC.md §4).

The preview and the sent submission are the SAME document — the preview
just has null submitted_at/confirmed_at. Tier decides which diagnose
layers are included; the raw-prompt inventory is only ever attached for
full_partnership, whose consent copy discloses it in plain words.
"""

from __future__ import annotations

from typing import Optional

from tryaii.cachelint._jsonutil import normalize_numbers


def build_submission(
    state: dict,
    diagnose_docs: Optional[dict],
    *,
    version: str,
    submitted_at,
    confirmed_at,
) -> dict:
    """diagnose_docs: None | {"run_id", "summary", "findings", "inventory"}
    (from the latest diagnose run). Field order is contractual."""
    consent = state["consent"]
    tier = consent["tier"]

    diagnose = None
    if tier != "contact_only" and diagnose_docs is not None:
        if tier == "summary_insights":
            diagnose = {
                "run_id": diagnose_docs["run_id"],
                "summary": diagnose_docs["summary"],
            }
        else:  # full_partnership
            diagnose = {
                "run_id": diagnose_docs["run_id"],
                "summary": diagnose_docs["summary"],
                "findings": diagnose_docs["findings"],
                "inventory": diagnose_docs["inventory"],
            }

    return normalize_numbers({
        "schema": "tryaii.designpartner.submission/1",
        "submitted_at": submitted_at,
        "tool": {"name": "tryaii", "version": version},
        "consent": {
            "tier": tier,
            "chosen_at": consent["chosen_at"],
            "confirmed_at": confirmed_at,
        },
        "answers": state["answers"],
        "diagnose": diagnose,
    })
