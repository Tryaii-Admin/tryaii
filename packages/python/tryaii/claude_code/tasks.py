"""Task texts of Claude Code calls, for the opt-in local analysis
(``tryaii claude-code analyze``, shared/monitor/SPEC.md §10.7).

This is the ONLY module of ``tryaii.claude_code`` that reads message text, and
only when the developer consented to ``analyze``. A task text lives in memory
just long enough to be embedded and classified on this machine; it is never
printed, stored, hashed or uploaded.

What counts as a task:

- **a main-thread turn**: the developer's own message (a ``type: "user"``
  line of a main session file that is not a sidechain line). Skipped, and
  counted: lines whose content is only ``tool_result`` parts (or that carry
  any ``tool_result`` part), ``isMeta`` / ``isCompactSummary`` lines, lines
  whose ``origin.kind`` is not ``human`` (task notifications, messages from
  other agents), and lines that are only injected context (system reminders,
  slash-command and local-command wrappers, ``[Request interrupted ...]``).
  ``<system-reminder>`` blocks are cut out of the text that is kept. The
  turn's calls are the main-thread assistant calls that follow it in the same
  file, up to the next turn.
- **a subagent run**: the instruction the subagent was started with, the
  first ``type: "user"`` line of the subagent file (or of one ``agentId`` for
  sidechain lines in a main file); its calls are every call of the run.

Only the text parts of a message are read, never tool inputs, tool results,
file contents or the full call context. The text is cut to
``MAX_PROMPT_LENGTH`` code points like the monitor's routing text (§2.1). A
text that looks like it holds a secret (``secret_scan.looks_secret``) is
dropped and counted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tryaii.classifiers.base import MAX_PROMPT_LENGTH
from tryaii.claude_code.reader import SYNTHETIC_MODEL, _is_subagent_file, log_files
from tryaii.claude_code.secret_scan import looks_secret

KIND_MAIN = "main"
KIND_SUBAGENT = "subagent"
_REMINDER = re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.DOTALL)
#: A user line whose text (after the reminders are cut) starts with one of these
#: is context Claude Code or a tool injected, not the developer's own message.
INJECTED_PREFIXES = (
    "<task-notification>", "<local-command-caveat>", "<local-command-stdout>",
    "<local-command-stderr>", "<command-name>", "<command-message>", "<command-args>",
    "<bash-input>", "<bash-stdout>", "<bash-stderr>", "<user-prompt-submit-hook>",
    "<side-sessions-event>", "[Request interrupted", "Caveat: The messages below",
)


@dataclass
class Task:
    """One task: its local text and the call keys it explains. ``ref`` is an
    internal grouping key (file and line, or the run key) and never leaves
    this process."""

    kind: str
    ref: str
    text: str
    call_keys: list = field(default_factory=list)


@dataclass
class TaskRead:
    tasks: list
    #: run key (``Call.run``) -> Task, for subagent runs with an instruction.
    runs: dict
    counts: dict


def _clean(text: str) -> str:
    return _REMINDER.sub("", text).strip()


def _content_text(content) -> tuple[Optional[str], bool]:
    """``(text, has_tool_result)`` of a user message's content: a string as is,
    a list's ``text`` parts joined with ``\\n``."""
    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return None, False
    parts, tool = [], False
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "tool_result":
            tool = True
        elif part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return ("\n".join(parts) if parts else None), tool


def _call_key(doc: dict) -> Optional[str]:
    message = doc.get("message")
    if not isinstance(message, dict) or message.get("model") == SYNTHETIC_MODEL:
        return None
    if not isinstance(message.get("usage"), dict):
        return None
    mid = message.get("id")
    if isinstance(mid, str) and mid:
        return mid
    rid = doc.get("requestId")
    return "req:" + rid if isinstance(rid, str) and rid else None


def _new_counts() -> dict:
    return {"main_turns": 0, "subagent_runs": 0, "skipped_tool_result_only": 0,
            "skipped_injected": 0, "skipped_empty": 0, "skipped_secret": 0,
            "runs_without_instruction": 0}


def _prompt_text(doc: dict, counts: dict) -> Optional[str]:
    """The developer's own text of a main-thread user line, or None (counted)."""
    message = doc.get("message")
    text, tool = _content_text(message.get("content") if isinstance(message, dict) else None)
    if tool:
        counts["skipped_tool_result_only"] += 1
        return None
    origin = doc.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if doc.get("isMeta") is True or doc.get("isCompactSummary") is True or \
            (kind is not None and kind != "human"):
        counts["skipped_injected"] += 1
        return None
    cleaned = _clean(text or "")
    if not cleaned:
        counts["skipped_empty" if not text else "skipped_injected"] += 1
        return None
    if cleaned.startswith(INJECTED_PREFIXES):
        counts["skipped_injected"] += 1
        return None
    return cleaned


def _instruction_text(doc: dict) -> Optional[str]:
    message = doc.get("message")
    text, _tool = _content_text(message.get("content") if isinstance(message, dict) else None)
    cleaned = _clean(text or "")
    return cleaned or None


def _keep(text: str, counts: dict) -> Optional[str]:
    if looks_secret(text):
        counts["skipped_secret"] += 1
        return None
    return text[:MAX_PROMPT_LENGTH]


def read_tasks(projects_dir) -> TaskRead:
    """Every main-thread turn and subagent run under ``projects_dir`` with its
    task text (local only). A call belongs to the first turn that precedes it
    in a file (a resumed session's copies do not move it)."""
    root = Path(projects_dir)
    tasks: list = []
    runs: dict = {}
    seen_runs: set = set()
    attached: set = set()
    counts = _new_counts()
    for path in log_files(root):
        sub_file = _is_subagent_file(path, root)
        file_run = "file:" + str(path) if sub_file else None
        current: Optional[Task] = None
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for number, line in enumerate(fh):
                if '"user"' not in line and '"assistant"' not in line:
                    continue
                try:
                    doc = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(doc, dict):
                    continue
                kind = doc.get("type")
                sidechain = sub_file or doc.get("isSidechain") is True
                if sidechain:
                    if kind != "user":
                        continue
                    run = file_run
                    if run is None:
                        agent_id = doc.get("agentId")
                        run = ("agent:" + agent_id if isinstance(agent_id, str) and agent_id
                               else "sidechain:" + str(path))
                    if run in seen_runs:
                        continue
                    seen_runs.add(run)
                    counts["subagent_runs"] += 1
                    text = _instruction_text(doc)
                    if text is None:
                        counts["runs_without_instruction"] += 1
                        continue
                    text = _keep(text, counts)
                    if text is not None:
                        task = Task(kind=KIND_SUBAGENT, ref=run, text=text)
                        runs[run] = task
                        tasks.append(task)
                    continue
                if kind == "user":
                    text = _prompt_text(doc, counts)
                    if text is None:
                        continue
                    counts["main_turns"] += 1
                    text = _keep(text, counts)
                    current = None
                    if text is not None:
                        current = Task(kind=KIND_MAIN, ref=f"{path}#{number}", text=text)
                        tasks.append(current)
                elif kind == "assistant" and current is not None:
                    key = _call_key(doc)
                    if key is not None and key not in attached:
                        attached.add(key)
                        current.call_keys.append(key)
    return TaskRead(tasks=tasks, runs=runs, counts=counts)
