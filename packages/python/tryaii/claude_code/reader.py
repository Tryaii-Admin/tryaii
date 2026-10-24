"""Read a developer's own Claude Code usage from its local session logs.

Claude Code writes one JSONL file per session under
``~/.claude/projects/<encoded project>/<session>.jsonl`` and one per subagent
under ``<session>/subagents/agent-<id>.jsonl`` (with a sibling
``agent-<id>.meta.json`` that names the subagent type). Usage sits on the
``type: "assistant"`` lines in ``message.usage``.

One API call spans several lines (one per content block, and a resumed
session copies earlier lines into a new file), so calls are deduplicated by
``message.id`` (fallback ``requestId``) across ALL files, taking the maximum
of every usage field; the call's time is the earliest line's ``timestamp``.
Lines of the model ``<synthetic>`` (written by Claude Code itself, no API
call) are skipped.

Privacy (shared/monitor/SPEC.md §10): only usage and metadata fields are read
from a parsed line -- ``type``, ``message.model``, ``message.id``,
``message.usage``, ``requestId``, ``timestamp``, ``isSidechain``, ``agentId``, ``cwd`` --
and only ``agentType`` from a meta file. Message text, tool inputs and outputs
never leave this module (the parsed line is dropped right away). The project
is the base name of the session's ``cwd`` (``tryaii-dev``), never a full path
and never the encoded folder name (it contains the OS user name, TS-226).

Each call also gets an internal ``session``: the name of the file holding the
call's earliest line (``<session>.jsonl``; for ``<session>/subagents/...`` the
``<session>`` folder). When copies tie (a resumed session copies earlier
lines with their times into a new file), the file whose calls run latest wins:
the continued session. It is a grouping key for ``sessions`` (cold cache
restarts) and is never output, stored or uploaded.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from tryaii.monitor.events import parse_ts

SYNTHETIC_MODEL = "<synthetic>"
SUBAGENTS_DIR = "subagents"
META_SUFFIX = ".meta.json"
#: Process names: the main conversation, and a subagent whose type is unknown.
MAIN_PROCESS = "main"
SUBAGENT_PROCESS = "subagent"
UNKNOWN_PROJECT = "unknown"
_PROCESS_MAX = 256
_PROJECT_MAX = 256

#: The usage fields kept per call (None = never reported on any line).
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens",
                "thinking_tokens")


def default_projects_dir(env=None) -> Path:
    """``$CLAUDE_CONFIG_DIR/projects`` when set, else ``~/.claude/projects``."""
    env = os.environ if env is None else env
    base = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def project_label(cwd) -> str:
    """The base name of a working directory (``C:\\x\\tryaii-dev`` -> ``tryaii-dev``);
    ``unknown`` when there is none. Both separators split, so a Windows path
    read on another OS is reduced too."""
    if not isinstance(cwd, str):
        return UNKNOWN_PROJECT
    parts = [p for p in re.split(r"[\\/]", cwd.strip()) if p]
    if not parts:
        return UNKNOWN_PROJECT
    name = parts[-1]
    if re.fullmatch(r"[A-Za-z]:", name) or name == "~":
        return UNKNOWN_PROJECT  # a drive root names no project
    return name[:_PROJECT_MAX]


def _count(value) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value)


@dataclass
class Call:
    """One deduplicated API call (metadata and token counts only)."""

    key: str
    model: str
    ts: datetime
    project: str
    subagent: bool
    agent_type: Optional[str]
    usage: dict = field(default_factory=dict)
    has_breakdown: bool = False
    #: The subagent run the call belongs to (internal grouping key, never output):
    #: the subagent file, or the line's ``agentId`` for a sidechain line elsewhere.
    run: Optional[str] = None
    files: int = 1
    #: The API request id (the line's ``requestId``): the event id is derived
    #: from it so a call imported from the logs and the same call received by
    #: live telemetry (SPEC.md §10.8) share one ``event_id``.
    request_id: Optional[str] = None
    #: The session (internal grouping key, NEVER output, stored or uploaded):
    #: see the module docstring.
    session: Optional[str] = None
    #: Set by ``sessions.mark``: True / False for a main-thread call of a known
    #: session (a cold cache restart or not), None otherwise.
    cache_cold: Optional[bool] = None
    #: The ``sessions.ColdRestart`` of a cold call, else None.
    cold: Optional[object] = None

    @property
    def process(self) -> str:
        if not self.subagent:
            return MAIN_PROCESS
        return self.agent_type or SUBAGENT_PROCESS

    def tokens(self, name: str) -> int:
        return self.usage.get(name) or 0

    @property
    def cache_write_tokens(self) -> int:
        """Cache write total: the aggregate field, else the breakdown's sum."""
        total = self.usage.get("cache_creation_input_tokens")
        if total is None:
            return self.tokens("cache_write_5m_tokens") + self.tokens("cache_write_1h_tokens")
        return total

    @property
    def context_tokens(self) -> int:
        """Every input token of the call: uncached + cache write + cache read."""
        return (self.tokens("input_tokens") + self.cache_write_tokens
                + self.tokens("cache_read_input_tokens"))


def _usage_of(raw: dict) -> tuple[dict, bool]:
    creation = raw.get("cache_creation")
    details = raw.get("output_tokens_details")
    out = {
        "input_tokens": _count(raw.get("input_tokens")),
        "output_tokens": _count(raw.get("output_tokens")),
        "cache_creation_input_tokens": _count(raw.get("cache_creation_input_tokens")),
        "cache_read_input_tokens": _count(raw.get("cache_read_input_tokens")),
        "cache_write_5m_tokens": None,
        "cache_write_1h_tokens": None,
        "thinking_tokens": None,
    }
    breakdown = isinstance(creation, dict)
    if breakdown:
        out["cache_write_5m_tokens"] = _count(creation.get("ephemeral_5m_input_tokens"))
        out["cache_write_1h_tokens"] = _count(creation.get("ephemeral_1h_input_tokens"))
    if isinstance(details, dict):
        out["thinking_tokens"] = _count(details.get("thinking_tokens"))
    return out, breakdown


def _merge_max(into: dict, other: dict) -> None:
    for name in USAGE_FIELDS:
        a, b = into.get(name), other.get(name)
        into[name] = b if a is None else (a if b is None else max(a, b))


def _agent_type(meta_path: Path) -> Optional[str]:
    """``agentType`` of a subagent meta file (nothing else is kept), or None."""
    try:
        with open(meta_path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    value = doc.get("agentType") if isinstance(doc, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:_PROCESS_MAX]


def log_files(projects_dir) -> list[Path]:
    """Every ``*.jsonl`` under the projects directory, sorted (deterministic)."""
    root = Path(projects_dir)
    if not root.is_dir():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(".jsonl"):
                out.append(Path(dirpath) / name)
    return sorted(out)


def _is_subagent_file(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return SUBAGENTS_DIR in parts[:-1]


def _session_of(path: Path, root: Path) -> str:
    """``<session>`` of ``<session>.jsonl`` or ``<session>/subagents/...``."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    dirs = parts[:-1]
    if SUBAGENTS_DIR in dirs:
        i = dirs.index(SUBAGENTS_DIR)
        if i > 0:
            return dirs[i - 1]
    return path.name[:-len(".jsonl")] if path.name.endswith(".jsonl") else path.stem


def _lines(path: Path) -> Iterable[dict]:
    """The metadata of the assistant lines of one file. A line is parsed only
    when it can be an assistant line with usage; every field other than the
    ones below is dropped as soon as the line is parsed."""
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            if '"assistant"' not in line or '"usage"' not in line:
                continue
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if not isinstance(doc, dict) or doc.get("type") != "assistant":
                continue
            message = doc.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            yield {
                "model": message.get("model"),
                "id": message.get("id"),
                "request_id": doc.get("requestId"),
                "usage": usage if isinstance(usage, dict) else None,
                "timestamp": doc.get("timestamp"),
                "sidechain": doc.get("isSidechain") is True,
                "agent_id": doc.get("agentId"),
                "cwd": doc.get("cwd"),
            }


def _assign_sessions(calls: dict, seen_in: dict) -> None:
    """Each call's session: the one holding its earliest line; on a tie the
    session whose calls run latest (a resumed copy continues the session)."""
    last: dict = {}
    for where in seen_in.values():
        for session, ts in where.items():
            if session not in last or ts > last[session]:
                last[session] = ts
    for key, call in calls.items():
        where = seen_in.get(key) or {}
        if not where:
            continue
        first = min(where.values())
        tied = [s for s, ts in where.items() if ts == first]
        call.session = max(tied, key=lambda s: (last[s], s))


@dataclass
class ReadResult:
    calls: list
    files: int
    lines: int
    duplicates: int
    synthetic: int


def read_calls(projects_dir, since: Optional[datetime] = None,
               until: Optional[datetime] = None) -> ReadResult:
    """Every deduplicated call under ``projects_dir`` whose time is within
    ``[since, until]`` (either bound optional), oldest first."""
    root = Path(projects_dir)
    files = log_files(root)
    calls: dict[str, Call] = {}
    meta_cache: dict[Path, Optional[str]] = {}
    lines = duplicates = synthetic = 0
    #: call key -> {session: earliest line time in that session's files}
    seen_in: dict[str, dict] = {}
    for path in files:
        sub_file = _is_subagent_file(path, root)
        session = _session_of(path, root)
        agent_type = None
        if sub_file:
            meta = path.with_name(path.name[:-len(".jsonl")] + META_SUFFIX)
            if meta not in meta_cache:
                meta_cache[meta] = _agent_type(meta)
            agent_type = meta_cache[meta]
        seen_here: set = set()
        for item in _lines(path):
            model = item["model"]
            if model == SYNTHETIC_MODEL:
                synthetic += 1
                continue
            key = item["id"] if isinstance(item["id"], str) and item["id"] else None
            if key is None and isinstance(item["request_id"], str) and item["request_id"]:
                key = "req:" + item["request_id"]
            if key is None or not isinstance(model, str) or not model or item["usage"] is None:
                continue
            try:
                ts = parse_ts(item["timestamp"])
            except ValueError:
                continue
            lines += 1
            usage, breakdown = _usage_of(item["usage"])
            subagent = sub_file or item["sidechain"]
            run = None
            if sub_file:
                run = "file:" + str(path)
            elif subagent:
                agent_id = item["agent_id"]
                run = ("agent:" + agent_id if isinstance(agent_id, str) and agent_id
                       else "sidechain:" + str(path))
            request_id = (item["request_id"] if isinstance(item["request_id"], str)
                          and item["request_id"] else None)
            where = seen_in.setdefault(key, {})
            if session not in where or ts < where[session]:
                where[session] = ts
            call = calls.get(key)
            if call is None:
                calls[key] = Call(key=key, model=model, ts=ts, project=project_label(item["cwd"]),
                                  subagent=subagent, agent_type=agent_type, usage=usage, run=run,
                                  has_breakdown=breakdown, request_id=request_id)
                seen_here.add(key)
                continue
            duplicates += 1
            _merge_max(call.usage, usage)
            call.has_breakdown = call.has_breakdown or breakdown
            call.ts = min(call.ts, ts)
            call.subagent = call.subagent or subagent
            call.agent_type = call.agent_type or agent_type
            call.run = call.run or run
            call.request_id = call.request_id or request_id
            if call.project == UNKNOWN_PROJECT:
                call.project = project_label(item["cwd"])
            if key not in seen_here:
                seen_here.add(key)
                call.files += 1
    _assign_sessions(calls, seen_in)
    from tryaii.claude_code.sessions import mark

    mark(list(calls.values()))  # over every call: a window's first call keeps its predecessor
    selected = [c for c in calls.values()
                if (since is None or c.ts >= since) and (until is None or c.ts <= until)]
    selected.sort(key=lambda c: (c.ts, c.key))
    return ReadResult(calls=selected, files=len(files), lines=lines, duplicates=duplicates,
                      synthetic=synthetic)
