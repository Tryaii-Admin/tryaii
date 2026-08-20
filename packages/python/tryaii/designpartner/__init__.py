"""designpartner — resumable design-partner enrollment.

One command (`tryaii designpartner`) drives questionnaire -> optional
diagnose run -> tiered consent -> confirmed submission. Contract:
shared/designpartner/SPEC.md. Nothing is sent without --confirm; every
submission is saved locally first.
"""

from tryaii.designpartner.api import DEFAULT_DIAGNOSE_OUT_DIR, advance
from tryaii.designpartner.catalog import (
    applicable_questions,
    load_catalog,
    tiers_by_id,
    validate_answers,
)
from tryaii.designpartner.payload import build_submission
from tryaii.designpartner.state import derive_stage, load_state
from tryaii.designpartner.transport import (
    DEFAULT_URL,
    effective_url,
    send_submission,
)

__all__ = [
    "DEFAULT_DIAGNOSE_OUT_DIR",
    "DEFAULT_URL",
    "advance",
    "applicable_questions",
    "build_submission",
    "derive_stage",
    "effective_url",
    "load_catalog",
    "load_state",
    "send_submission",
    "tiers_by_id",
    "validate_answers",
]
