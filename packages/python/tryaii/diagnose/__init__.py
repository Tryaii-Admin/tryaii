"""diagnose — agent-first codebase diagnostics (insight-only).

The coding agent discovers LLM call sites and writes an inventory JSON;
this package runs the deterministic checks over it (SPEC:
shared/diagnose/SPEC.md) and persists runs under .tryaii/diagnose/.
"""

from tryaii.diagnose.api import DEFAULT_CHECKS, analyze_inventory
from tryaii.diagnose.intake import normalize_inventory
from tryaii.diagnose.report import render_report_html
from tryaii.diagnose.resolve import resolve_model_id
from tryaii.diagnose.store import (
    latest_run_id,
    list_run_ids,
    load_run_findings,
    previous_run_id,
    write_run,
)

__all__ = [
    "DEFAULT_CHECKS",
    "analyze_inventory",
    "normalize_inventory",
    "render_report_html",
    "resolve_model_id",
    "write_run",
    "latest_run_id",
    "previous_run_id",
    "list_run_ids",
    "load_run_findings",
]
