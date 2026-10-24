"""Your own Claude Code usage: read from the local session logs, reported, and
imported as monitor events of the app ``claude-code`` (shared/monitor/SPEC.md
§10). Python only for now. Never reads message text into a report or event;
the opt-in ``analyze`` (``tasks`` + ``analysis``, §10.7) reads task text on
this machine to classify it and keeps only the classification.
"""

from tryaii.claude_code.importer import APP, build_call_event, event_id
from tryaii.claude_code.pricing import Pricer
from tryaii.claude_code.reader import Call, default_projects_dir, project_label, read_calls
from tryaii.claude_code.report import build_report, render_report

__all__ = [
    "APP",
    "Call",
    "Pricer",
    "build_call_event",
    "build_report",
    "default_projects_dir",
    "event_id",
    "project_label",
    "read_calls",
    "render_report",
]
